from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException, Request
from slack_sdk.signature import SignatureVerifier
from slack_sdk.web.async_client import AsyncWebClient

from ..integrations import Integration
from ..logging import setup_logger
from ..loop import LoopEvent, LoopState
from ..types import IntegrationType, SlackConfig

if TYPE_CHECKING:
    from ..fastloop import FastLoop

logger = setup_logger(__name__)


class SlackMessageEvent(LoopEvent):
    type: str = "slack_message"
    channel: str
    user: str
    text: str
    ts: str
    thread_ts: str | None = None
    team: str
    event_ts: str


class SlackReactionEvent(LoopEvent):
    type: str = "slack_reaction"
    channel: str
    user: str
    reaction: str
    item_user: str
    item: dict[str, Any]
    event_ts: str


class SlackAppMentionEvent(LoopEvent):
    type: str = "slack_app_mention"
    channel: str
    user: str
    text: str
    ts: str
    thread_ts: str | None = None
    team: str
    event_ts: str


class SlackIntegration(Integration):
    def __init__(
        self,
        *,
        app_id: str,
        bot_token: str,
        signing_secret: str,
        client_id: str,
    ):
        super().__init__()

        self.config = SlackConfig(
            app_id=app_id,
            bot_token=bot_token,
            signing_secret=signing_secret,
            client_id=client_id,
        )

        self.client: AsyncWebClient = AsyncWebClient(token=self.config.bot_token)
        self.verifier: SignatureVerifier = SignatureVerifier(self.config.signing_secret)

    def type(self) -> IntegrationType:
        return IntegrationType.SLACK

    def register(self, fastloop: "FastLoop", loop_name: str) -> None:
        fastloop.register_events(
            [
                SlackMessageEvent,
                SlackAppMentionEvent,
                SlackReactionEvent,
            ]
        )

        self._fastloop: FastLoop = fastloop
        self._fastloop.app.add_api_route(
            path=f"/{loop_name}/slack/events",
            endpoint=self._handle_slack_event,
            methods=["POST"],
            response_model=None,
        )
        self.loop_name: str = loop_name

    def _ok(self) -> dict[str, Any]:
        return {"ok": True}

    async def _handle_slack_event(self, request: Request):
        body = await request.body()

        if not self.verifier.is_valid_request(body, dict(request.headers)):
            raise HTTPException(
                status_code=HTTPStatus.FORBIDDEN, detail="Invalid signature"
            )

        payload = await request.json()
        if payload.get("type") == "url_verification":
            return {"challenge": payload["challenge"]}

        event = payload.get("event", {})
        event_type = event.get("type")

        if event_type not in ("message", "app_mention", "reaction_added"):
            return self._ok()

        thread_ts = event.get("thread_ts") or event.get("ts")
        channel = event.get("channel")
        user = event.get("user")
        text = event.get("text", "")
        team = event.get("team") or payload.get("team_id")
        event_ts = event.get("event_ts")
        reaction = event.get("reaction")
        item_user = event.get("item_user")
        item = event.get("item")

        loop_id = await self._fastloop.state_manager.get_loop_mapping(
            f"slack_thread:{channel}:{thread_ts}"
        )

        loop_event_handler = self._fastloop.loop_event_handlers.get(self.loop_name)
        if not loop_event_handler:
            return self._ok()

        loop_event: LoopEvent | None = None
        if event_type == "app_mention":
            loop_event = SlackAppMentionEvent(
                loop_id=loop_id or None,
                channel=channel,
                user=user,
                text=text,
                ts=thread_ts,
                team=team,
                event_ts=event_ts,
            )
        elif event_type == "message":
            loop_event = SlackMessageEvent(
                loop_id=loop_id or None,
                channel=channel,
                user=user,
                text=text,
                ts=thread_ts,
                team=team,
                event_ts=event_ts,
            )
        elif event_type == "reaction_added":
            loop_event = SlackReactionEvent(
                loop_id=loop_id or None,
                channel=channel,
                user=user,
                reaction=reaction,
                item_user=item_user,
                item=item,
                event_ts=event_ts,
            )

        mapped_request: dict[str, Any] = loop_event.to_dict() if loop_event else {}

        loop: LoopState = await loop_event_handler(mapped_request)
        if loop.loop_id:
            await self._fastloop.state_manager.set_loop_mapping(
                f"slack_thread:{channel}:{thread_ts}", loop.loop_id
            )

        return self._ok()

    def events(self) -> list[Any]:
        return [SlackMessageEvent, SlackAppMentionEvent, SlackReactionEvent]

    async def emit(self, event: Any) -> None:
        _event: SlackMessageEvent | SlackAppMentionEvent | SlackReactionEvent = cast(
            "SlackMessageEvent | SlackAppMentionEvent | SlackReactionEvent", event
        )

        if isinstance(_event, SlackMessageEvent):
            await self.client.chat_postMessage(  # type: ignore
                channel=_event.channel, text=_event.text, thread_ts=_event.thread_ts
            )

        elif isinstance(_event, SlackReactionEvent):
            await self.client.reactions_add(  # type: ignore
                channel=_event.channel,
                name=_event.reaction,
                timestamp=_event.event_ts,
                item_user=_event.item_user,
                item=_event.item,
            )

        elif isinstance(_event, SlackAppMentionEvent):  # type: ignore
            await self.client.chat_postMessage(  # type: ignore
                channel=_event.channel,
                text=_event.text,
                thread_ts=_event.thread_ts,
            )
