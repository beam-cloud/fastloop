from http import HTTPStatus
from typing import TYPE_CHECKING, Any

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
        client_secret: str,
        verification_token: str,
    ):
        super().__init__()
        self.config = SlackConfig(
            app_id=app_id,
            bot_token=bot_token,
            signing_secret=signing_secret,
            client_id=client_id,
            client_secret=client_secret,
            verification_token=verification_token,
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
            return {"ok": True}

        thread_ts = event.get("thread_ts") or event.get("ts")
        channel = event.get("channel")
        user = event.get("user")
        text = event.get("text", "")
        team = event.get("team") or payload.get("team_id")
        event_ts = event.get("event_ts")

        loop_id = await self._fastloop.state_manager.get_loop_mapping(
            f"slack_thread:{channel}:{thread_ts}"
        )

        loop_event_handler = self._fastloop.loop_event_handlers.get(self.loop_name)
        if not loop_event_handler:
            return {"ok": True}

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

        mapped_request: dict[str, Any] = loop_event.to_dict() if loop_event else {}

        loop: LoopState = await loop_event_handler(mapped_request)
        if loop.loop_id:
            await self._fastloop.state_manager.set_loop_mapping(
                f"slack_thread:{channel}:{thread_ts}", loop.loop_id
            )

        return {"ok": True}

    async def send_message(self, channel: str, text: str, thread_ts: str | None = None):
        await self.client.chat_postMessage(  # type: ignore
            channel=channel, text=text, thread_ts=thread_ts
        )
