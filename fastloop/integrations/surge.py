from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

import httpx
from fastapi import Request

from ..integrations import Integration
from ..logging import setup_logger
from ..models import LoopEvent, LoopState
from ..types import IntegrationType

if TYPE_CHECKING:
    from ..context import LoopContext
    from ..fastloop import FastLoop

logger = setup_logger(__name__)

SurgeSetupCallback = Callable[["LoopContext", "LoopEvent"], Awaitable["SurgeConfig"]]


class SurgeConfig:
    def __init__(self, token: str, account_id: str):
        self.token = token
        self.account_id = account_id


class SurgeRxMessageEvent(LoopEvent):
    type: str = "surge_rx_message"
    message_id: str
    body: str
    conversation_id: str
    contact_id: str
    contact_first_name: str
    contact_last_name: str
    contact_phone_number: str
    contact_email: str | None = None
    received_at: str
    event_id: str
    surge_timestamp: str


class SurgeTxMessageEvent(LoopEvent):
    type: str = "surge_tx_message"
    body: str
    first_name: str
    last_name: str
    phone_number: str


SUPPORTED_SURGE_EVENTS = ["message.received"]


class SurgeIntegration(Integration):
    BASE_URL = "https://api.surge.app"

    def __init__(self, *, setup: SurgeSetupCallback):
        super().__init__()
        self._setup_callback = setup

    def type(self) -> IntegrationType:
        return IntegrationType.SURGE

    async def setup_for_context(
        self, context: "LoopContext", event: "LoopEvent"
    ) -> httpx.AsyncClient:
        config = await self._setup_callback(context, event)
        client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {config.token}",
                "Content-Type": "application/json",
            },
        )
        context.set_integration_client(self.type(), client)
        context.set_integration_client(f"{self.type()}_config", config)
        return client

    def get_client_for_context(self, context: "LoopContext") -> httpx.AsyncClient:
        client = context.get_integration_client(self.type())
        if client is None:
            raise ValueError(
                "Surge client not initialized for this context. "
                "Ensure setup_for_context was called."
            )
        return cast("httpx.AsyncClient", client)

    def get_config_for_context(self, context: "LoopContext") -> SurgeConfig:
        config = context.get_integration_client(f"{self.type()}_config")
        if config is None:
            raise ValueError("Surge config not initialized for this context.")
        return cast("SurgeConfig", config)

    def register(self, fastloop: "FastLoop", loop_name: str) -> None:
        fastloop.register_events([SurgeRxMessageEvent, SurgeTxMessageEvent])

        self._fastloop: FastLoop = fastloop
        self._fastloop.add_api_route(
            path=f"/{loop_name}/surge/events",
            endpoint=self._handle_surge_event,
            methods=["POST"],
            response_model=None,
        )
        self.loop_name: str = loop_name

    def _ok(self) -> dict[str, Any]:
        return {"ok": True}

    async def _handle_surge_event(self, request: Request):
        payload = await request.json()

        event_type = payload.get("type")
        if event_type not in SUPPORTED_SURGE_EVENTS:
            return self._ok()

        if event_type == "message.received":
            data = payload.get("data", {})
            conversation = data.get("conversation", {})
            contact = conversation.get("contact", {})

            conversation_id = conversation.get("id", "")

            loop_id = await self._fastloop.state_manager.get_loop_mapping(
                f"surge_conversation:{conversation_id}"
            )

            loop_event_handler = self._fastloop.loop_event_handlers.get(self.loop_name)
            if not loop_event_handler:
                return self._ok()

            loop_event = SurgeRxMessageEvent(
                loop_id=loop_id or None,
                message_id=data.get("id", ""),
                body=data.get("body", ""),
                conversation_id=conversation_id,
                contact_id=contact.get("id", ""),
                contact_first_name=contact.get("first_name", ""),
                contact_last_name=contact.get("last_name", ""),
                contact_phone_number=contact.get("phone_number", ""),
                contact_email=contact.get("email"),
                received_at=data.get("received_at", ""),
                event_id=payload.get("id", ""),
                surge_timestamp=payload.get("timestamp", ""),
            )

            mapped_request: dict[str, Any] = loop_event.to_dict()
            loop: LoopState = await loop_event_handler(mapped_request)
            if loop.loop_id:
                await self._fastloop.state_manager.set_loop_mapping(
                    f"surge_conversation:{conversation_id}", loop.loop_id
                )

        return self._ok()

    def events(self) -> list[Any]:
        return [SurgeRxMessageEvent, SurgeTxMessageEvent]

    async def emit(self, event: Any, context: "LoopContext | None" = None) -> None:
        if context is None:
            raise ValueError("Context is required for Surge integration")

        client = self.get_client_for_context(context)
        config = self.get_config_for_context(context)

        if isinstance(event, SurgeTxMessageEvent):
            payload = {
                "body": event.body,
                "conversation": {
                    "contact": {
                        "first_name": event.first_name,
                        "last_name": event.last_name,
                        "phone_number": event.phone_number,
                    }
                },
            }

            response = await client.post(
                f"/accounts/{config.account_id}/messages",
                json=payload,
            )
            response.raise_for_status()
