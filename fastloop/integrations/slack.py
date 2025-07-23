from typing import TYPE_CHECKING, Any

from slack_sdk.signature import SignatureVerifier
from slack_sdk.web.async_client import AsyncWebClient

from ..integrations import Integration
from ..logging import setup_logger
from ..loop import LoopEvent
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
    def __init__(self, config: SlackConfig):
        self.config: SlackConfig = config
        self.client: AsyncWebClient = AsyncWebClient(token=config.bot_token)
        self.verifier: SignatureVerifier = SignatureVerifier(config.signing_secret)

    def handle_event(self, event: LoopEvent):
        pass

    def type(self) -> IntegrationType:
        return IntegrationType.SLACK

    def register(self, fastloop: "FastLoop") -> None:
        fastloop.register_events(
            [
                SlackMessageEvent,
                SlackAppMentionEvent,
                SlackReactionEvent,
            ]
        )

        # fastloop.app.add_api_route(
        #     "/slack/events",
        #     self.handle_event,
        #     methods=["POST"],
        # )


def create_slack_integration(
    *,
    app_id: str,
    bot_token: str,
    signing_secret: str,
    client_id: str,
    client_secret: str,
    verification_token: str,
) -> Integration:
    return SlackIntegration(
        SlackConfig(
            app_id=app_id,
            bot_token=bot_token,
            signing_secret=signing_secret,
            client_id=client_id,
            client_secret=client_secret,
            verification_token=verification_token,
        )
    )
