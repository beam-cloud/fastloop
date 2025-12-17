import re
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException, Request
from pydantic import Field
from slack_sdk.signature import SignatureVerifier
from slack_sdk.web.async_client import AsyncWebClient

from ..integrations import Integration
from ..models import LoopEvent, LoopState
from ..types import IntegrationType

if TYPE_CHECKING:
    from ..context import LoopContext
    from ..fastloop import FastLoop

SlackSetupCallback = Callable[["LoopContext", "LoopEvent"], Awaitable[str]]

IGNORED_MESSAGE_SUBTYPES = frozenset(
    [
        "bot_message",
        "message_changed",
        "message_deleted",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "group_join",
        "group_leave",
        "group_topic",
        "group_purpose",
        "group_name",
        "group_archive",
        "group_unarchive",
    ]
)

URL_PATTERN = re.compile(r"<(https?://[^|>]+)(?:\|[^>]*)?>|(?<![<|])(https?://\S+)")


async def _download_slack_file(
    context: "LoopContext", file_id: str, url: str | None = None
) -> bytes:
    from aiohttp import ClientSession

    integration = context.integrations.get(IntegrationType.SLACK)
    if integration is None:
        raise ValueError("Slack integration not found in context")

    client = integration.get_client_for_context(context)
    download_url = url

    if not download_url:
        file_info = await client.files_info(file=file_id)
        file_obj = file_info.get("file", {})
        download_url = file_obj.get("url_private_download") or file_obj.get(
            "url_private"
        )

    if not download_url:
        raise ValueError(f"No download URL found for file {file_id}")

    headers = {"Authorization": f"Bearer {client.token}"}
    async with (
        ClientSession() as session,
        session.get(download_url, headers=headers) as resp,
    ):
        resp.raise_for_status()
        return await resp.read()


def _extract_urls(text: str | None) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    return [
        url
        for match in URL_PATTERN.finditer(text)
        if (url := match.group(1) or match.group(2))
        and url not in seen
        and not seen.add(url)
    ]


def _parse_slack_files(files_raw: list[dict[str, Any]] | None) -> list["SlackFile"]:
    if not files_raw:
        return []
    return [
        SlackFile(
            id=f.get("id", ""),
            name=f.get("name"),
            mimetype=f.get("mimetype"),
            size=f.get("size"),
            url_private=f.get("url_private"),
            url_private_download=f.get("url_private_download"),
            permalink=f.get("permalink"),
        )
        for f in files_raw
    ]


class SlackFile(LoopEvent):
    type: str = "slack_file"
    id: str
    name: str | None = None
    mimetype: str | None = None
    size: int | None = None
    url_private: str | None = None
    url_private_download: str | None = None
    permalink: str | None = None

    async def download(self, context: "LoopContext") -> bytes:
        return await _download_slack_file(
            context, self.id, self.url_private_download or self.url_private
        )


class SlackRichMessageEvent(LoopEvent):
    channel: str
    user: str
    text: str
    ts: str
    thread_ts: str | None = None
    team: str
    event_ts: str
    files: list[SlackFile] = Field(default_factory=list)
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    raw_event: dict[str, Any] | None = None

    @property
    def root_ts(self) -> str:
        return self.thread_ts or self.ts

    async def download_file(self, context: "LoopContext", file_id: str) -> bytes:
        for f in self.files:
            if f.id == file_id:
                return await f.download(context)
        raise ValueError(f"File {file_id} not found in message")


class SlackMessageEvent(SlackRichMessageEvent):
    type: str = "slack_message"
    subtype: str | None = None
    bot_id: str | None = None


class SlackAppMentionEvent(SlackRichMessageEvent):
    type: str = "slack_app_mention"


class SlackReactionEvent(LoopEvent):
    type: str = "slack_reaction"
    channel: str
    user: str
    reaction: str
    item_user: str
    item: dict[str, Any] | None = None
    event_ts: str


class SlackFileSharedEvent(LoopEvent):
    type: str = "slack_file_shared"
    file_id: str
    user: str
    channel: str
    event_ts: str

    async def download_file(self, context: "LoopContext") -> bytes:
        return await _download_slack_file(context, self.file_id)


class SlackLinkSharedEvent(LoopEvent):
    type: str = "slack_link_shared"
    channel: str
    user: str
    message_ts: str
    thread_ts: str | None = None
    links: list[dict[str, Any]] = Field(default_factory=list)
    event_ts: str

    @property
    def root_ts(self) -> str:
        return self.thread_ts or self.message_ts

    @property
    def urls(self) -> list[str]:
        return [link["url"] for link in self.links if link.get("url")]


class SlackFileUploadEvent(LoopEvent):
    type: str = "slack_file_upload"
    channel: str
    thread_ts: str | None = None
    filename: str
    content: bytes | None = None
    file_path: str | None = None
    title: str | None = None
    initial_comment: str | None = None


SLACK_EVENT_TYPES: list[type[LoopEvent]] = [
    SlackMessageEvent,
    SlackAppMentionEvent,
    SlackReactionEvent,
    SlackFileSharedEvent,
    SlackLinkSharedEvent,
    SlackFileUploadEvent,
]

SUPPORTED_SLACK_EVENTS = [
    "message",
    "app_mention",
    "reaction_added",
    "file_shared",
    "link_shared",
]


class SlackIntegration(Integration):
    def __init__(self, *, signing_secret: str, setup: SlackSetupCallback):
        super().__init__()
        self._setup_callback = setup
        self._signing_secret = signing_secret
        self.verifier = SignatureVerifier(signing_secret)

    def type(self) -> IntegrationType:
        return IntegrationType.SLACK

    async def setup_for_context(
        self, context: "LoopContext", event: "LoopEvent"
    ) -> AsyncWebClient:
        bot_token = await self._setup_callback(context, event)
        client = AsyncWebClient(token=bot_token)
        context.set_integration_client(self.type(), client)
        return client

    def get_client_for_context(self, context: "LoopContext") -> AsyncWebClient:
        client = context.get_integration_client(self.type())
        if client is None:
            raise ValueError(
                "Slack client not initialized for this context. "
                "Ensure setup_for_context was called."
            )
        return cast("AsyncWebClient", client)

    def register(self, fastloop: "FastLoop", loop_name: str) -> None:
        fastloop.register_events(SLACK_EVENT_TYPES)
        self._fastloop: FastLoop = fastloop
        self._fastloop.add_api_route(
            path=f"/{loop_name}/slack/events",
            endpoint=self._handle_slack_event,
            methods=["POST"],
            response_model=None,
        )
        self.loop_name: str = loop_name

    def events(self) -> list[Any]:
        return list(SLACK_EVENT_TYPES)

    async def _handle_slack_event(self, request: Request):
        body = await request.body()
        if not self.verifier.is_valid_request(body, dict(request.headers)):
            raise HTTPException(
                status_code=HTTPStatus.FORBIDDEN, detail="Invalid signature"
            )

        payload = await request.json()
        if payload.get("type") == "url_verification":
            return {"challenge": payload["challenge"]}

        event: dict[str, Any] = payload.get("event", {})
        event_type = event.get("type")

        if event_type not in SUPPORTED_SLACK_EVENTS:
            return {"ok": True}

        if event_type == "message" and (
            event.get("subtype") in IGNORED_MESSAGE_SUBTYPES or event.get("bot_id")
        ):
            return {"ok": True}

        handler = self._fastloop.loop_event_handlers.get(self.loop_name)
        if not handler:
            return {"ok": True}

        channel = event.get("channel", "")
        root_ts = event.get("thread_ts") or event.get("ts", "")
        loop_id = await self._fastloop.state_manager.get_loop_mapping(
            f"slack_thread:{channel}:{root_ts}"
        )

        loop_event = self._map_event(event, event_type, payload, loop_id)
        if loop_event is None:
            return {"ok": True}

        loop: LoopState = await handler(loop_event.to_dict())
        if loop.loop_id:
            await self._fastloop.state_manager.set_loop_mapping(
                f"slack_thread:{channel}:{root_ts}", loop.loop_id
            )

        return {"ok": True}

    def _map_event(
        self,
        event: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        loop_id: str | None,
    ) -> LoopEvent | None:
        channel = event.get("channel", "")
        base = {
            "loop_id": loop_id,
            "channel": channel,
            "event_ts": event.get("event_ts", ""),
        }

        if event_type in ("app_mention", "message"):
            rich_fields = {
                **base,
                "user": event.get("user", ""),
                "text": event.get("text", ""),
                "ts": event.get("ts", ""),
                "thread_ts": event.get("thread_ts"),
                "team": event.get("team", "") or payload.get("team_id", ""),
                "files": _parse_slack_files(event.get("files")),
                "blocks": event.get("blocks", []),
                "attachments": event.get("attachments", []),
                "links": _extract_urls(event.get("text")),
                "raw_event": event,
            }
            if event_type == "app_mention":
                return SlackAppMentionEvent(**rich_fields)
            return SlackMessageEvent(
                **rich_fields, subtype=event.get("subtype"), bot_id=event.get("bot_id")
            )

        if event_type == "reaction_added":
            return SlackReactionEvent(
                **base,
                user=event.get("user", ""),
                reaction=event.get("reaction", ""),
                item_user=event.get("item_user", ""),
                item=cast("dict[str, Any]", event.get("item")),
            )

        if event_type == "file_shared":
            return SlackFileSharedEvent(
                **base, file_id=event.get("file_id", ""), user=event.get("user", "")
            )

        if event_type == "link_shared":
            return SlackLinkSharedEvent(
                **base,
                user=event.get("user", ""),
                message_ts=event.get("message_ts", event.get("ts", "")),
                thread_ts=event.get("thread_ts"),
                links=event.get("links", []),
            )

        return None

    async def emit(
        self, event: LoopEvent, context: "LoopContext | None" = None
    ) -> None:
        if context is None:
            raise ValueError("Context is required for Slack integration")

        client = self.get_client_for_context(context)

        if isinstance(event, SlackRichMessageEvent):
            kwargs: dict[str, Any] = {"channel": event.channel, "text": event.text}
            if event.thread_ts:
                kwargs["thread_ts"] = event.thread_ts
            if event.blocks:
                kwargs["blocks"] = event.blocks
            if event.attachments:
                kwargs["attachments"] = event.attachments
            await client.chat_postMessage(**kwargs)

        elif isinstance(event, SlackReactionEvent):
            await client.reactions_add(
                channel=event.channel, name=event.reaction, timestamp=event.event_ts
            )

        elif isinstance(event, SlackFileUploadEvent):
            if event.content is None and not event.file_path:
                raise ValueError(
                    "SlackFileUploadEvent requires either content or file_path"
                )
            kwargs = {"channels": event.channel, "filename": event.filename}
            if event.thread_ts:
                kwargs["thread_ts"] = event.thread_ts
            if event.title:
                kwargs["title"] = event.title
            if event.initial_comment:
                kwargs["initial_comment"] = event.initial_comment
            kwargs["content" if event.content is not None else "file"] = (
                event.content if event.content is not None else event.file_path
            )
            await client.files_upload_v2(**kwargs)

        elif isinstance(event, (SlackFileSharedEvent, SlackLinkSharedEvent)):
            raise NotImplementedError(f"{type(event).__name__} is inbound-only.")
