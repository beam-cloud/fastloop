from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

import httpx
from fastapi import Request
from pydantic import Field

from ..integrations import Integration
from ..logging import setup_logger
from ..models import LoopEvent, LoopState
from ..types import IntegrationType

if TYPE_CHECKING:
    from ..context import LoopContext
    from ..fastloop import FastLoop

logger = setup_logger(__name__)

TelnyxSetupCallback = Callable[["LoopContext", "LoopEvent"], Awaitable["TelnyxConfig"]]


class TelnyxConfig:
    def __init__(
        self,
        api_key: str,
        default_from: str | None = None,
        messaging_profile_id: str | None = None,
    ):
        self.api_key = api_key
        self.default_from = default_from
        self.messaging_profile_id = messaging_profile_id


class TelnyxRxMessageEvent(LoopEvent):
    type: str = "telnyx_rx_message"
    event_type: str
    message_id: str
    direction: str
    text: str
    from_number: str
    to_numbers: list[str]
    media: list[dict[str, Any]] = Field(default_factory=list)
    messaging_profile_id: str | None = None
    organization_id: str | None = None
    received_at: str | None = None
    tags: list[str] = Field(default_factory=list)
    subject: str | None = None
    raw_payload: dict[str, Any]


class TelnyxTxMessageEvent(LoopEvent):
    type: str = "telnyx_tx_message"
    to: str
    text: str
    from_number: str | None = None
    messaging_profile_id: str | None = None
    subject: str | None = None
    media_urls: list[str] | None = None
    use_profile_webhooks: bool = True
    webhook_url: str | None = None
    webhook_failover_url: str | None = None


class TelnyxIntegration(Integration):
    BASE_URL = "https://api.telnyx.com/v2"

    def __init__(self, *, setup: TelnyxSetupCallback):
        super().__init__()
        self._setup_callback = setup

    def type(self) -> IntegrationType:
        return IntegrationType.TELNYX

    async def setup_for_context(
        self, context: "LoopContext", event: "LoopEvent"
    ) -> httpx.AsyncClient:
        config = await self._setup_callback(context, event)
        client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        context.set_integration_client(self.type(), client)
        context.set_integration_client(f"{self.type()}_config", config)
        return client

    def get_client_for_context(self, context: "LoopContext") -> httpx.AsyncClient:
        client = context.get_integration_client(self.type())
        if client is None:
            raise ValueError(
                "Telnyx client not initialized for this context. "
                "Ensure setup_for_context was called."
            )
        return cast("httpx.AsyncClient", client)

    def get_config_for_context(self, context: "LoopContext") -> TelnyxConfig:
        config = context.get_integration_client(f"{self.type()}_config")
        if config is None:
            raise ValueError("Telnyx config not initialized for this context.")
        return cast("TelnyxConfig", config)

    def register(self, fastloop: "FastLoop", loop_name: str) -> None:
        fastloop.register_events([TelnyxRxMessageEvent, TelnyxTxMessageEvent])

        self._fastloop: FastLoop = fastloop
        self._fastloop.add_api_route(
            path=f"/{loop_name}/telnyx/events",
            endpoint=self._handle_telnyx_event,
            methods=["POST"],
            response_model=None,
        )
        self.loop_name: str = loop_name

    def _ok(self) -> dict[str, Any]:
        return {"ok": True}

    async def _handle_telnyx_event(self, request: Request):
        payload = await request.json()
        data = payload.get("data", {})
        event_type = data.get("event_type", payload.get("event_type", "unknown"))

        if event_type != "message.received":
            return self._ok()

        inner_payload = data.get("payload", {})

        message_id = inner_payload.get("id") or data.get("id") or ""
        direction = inner_payload.get("direction") or ""
        text = inner_payload.get("text") or ""

        from_obj = inner_payload.get("from", {})
        from_number = (
            from_obj.get("phone_number")
            if isinstance(from_obj, dict)
            else str(from_obj)
        )

        to_list = inner_payload.get("to", [])
        to_numbers = []
        if isinstance(to_list, list):
            for t in to_list:
                if isinstance(t, dict):
                    if "phone_number" in t:
                        to_numbers.append(t["phone_number"])
                else:
                    to_numbers.append(str(t))

        media = inner_payload.get("media", [])
        messaging_profile_id = inner_payload.get("messaging_profile_id")
        organization_id = inner_payload.get("organization_id")
        received_at = inner_payload.get("received_at")
        tags = inner_payload.get("tags", [])
        subject = inner_payload.get("subject")

        to_number = to_numbers[0] if to_numbers else ""
        loop_id = await self._fastloop.state_manager.get_loop_mapping(
            f"telnyx_conversation:{from_number}:{to_number}"
        )

        loop_event_handler = self._fastloop.loop_event_handlers.get(self.loop_name)
        if not loop_event_handler:
            return self._ok()

        loop_event = TelnyxRxMessageEvent(
            loop_id=loop_id or None,
            event_type=event_type,
            message_id=message_id,
            direction=direction,
            text=text,
            from_number=from_number or "",
            to_numbers=to_numbers,
            media=media,
            messaging_profile_id=messaging_profile_id,
            organization_id=organization_id,
            received_at=received_at,
            tags=tags,
            subject=subject,
            raw_payload=payload,
        )

        mapped_request: dict[str, Any] = loop_event.to_dict()
        loop: LoopState = await loop_event_handler(mapped_request)
        if loop.loop_id:
            await self._fastloop.state_manager.set_loop_mapping(
                f"telnyx_conversation:{from_number}:{to_number}", loop.loop_id
            )

        return self._ok()

    def events(self) -> list[Any]:
        return [TelnyxRxMessageEvent, TelnyxTxMessageEvent]

    async def emit(self, event: Any, context: "LoopContext | None" = None) -> None:
        if context is None:
            raise ValueError("Context is required for Telnyx integration")

        client = self.get_client_for_context(context)
        config = self.get_config_for_context(context)

        if isinstance(event, TelnyxTxMessageEvent):
            payload: dict[str, Any] = {
                "to": event.to,
                "text": event.text,
                "use_profile_webhooks": event.use_profile_webhooks,
            }

            from_val = event.from_number or config.default_from
            profile_id_val = event.messaging_profile_id or config.messaging_profile_id

            if from_val:
                payload["from"] = from_val
            if profile_id_val:
                payload["messaging_profile_id"] = profile_id_val

            logger.info(
                "Sending Telnyx message",
                extra={
                    "to": event.to,
                    "from": from_val,
                    "messaging_profile_id": profile_id_val,
                    "text": event.text,
                },
            )

            if event.subject:
                payload["subject"] = event.subject

            if event.media_urls:
                payload["media_urls"] = event.media_urls
                payload["type"] = "MMS"
            else:
                payload["type"] = "SMS"

            if event.webhook_url:
                payload["webhook_url"] = event.webhook_url
            if event.webhook_failover_url:
                payload["webhook_failover_url"] = event.webhook_failover_url

            response = await client.post("/messages", json=payload)
            if response.is_error:
                logger.error(
                    "Telnyx API error",
                    extra={
                        "status_code": response.status_code,
                        "response_body": response.text,
                    },
                )
            response.raise_for_status()
