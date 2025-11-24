from typing import TYPE_CHECKING, Any, cast

import httpx
from fastapi import Request

from ..integrations import Integration
from ..logging import setup_logger
from ..loop import LoopEvent
from ..types import IntegrationType, TelnyxConfig

if TYPE_CHECKING:
    from ..fastloop import FastLoop

logger = setup_logger(__name__)


class TelnyxRxMessageEvent(LoopEvent):
    type: str = "telnyx_rx_message"
    event_type: str
    payload: dict[str, Any]


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
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.telnyx.com/v2",
        default_from: str | None = None,
        messaging_profile_id: str | None = None,
    ):
        super().__init__()

        self.config = TelnyxConfig(
            api_key=api_key,
            base_url=base_url,
            default_from=default_from,
            messaging_profile_id=messaging_profile_id,
        )

        self.client = httpx.AsyncClient(
            base_url=self.config.base_url,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def type(self) -> IntegrationType:
        return IntegrationType.TELNYX

    def register(self, fastloop: "FastLoop", loop_name: str) -> None:
        fastloop.register_events(
            [
                TelnyxRxMessageEvent,
                TelnyxTxMessageEvent,
            ]
        )

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
        # Try to extract event type from common locations in Telnyx webhooks
        # Usually it's in data.event_type
        data = payload.get("data", {})
        event_type = data.get("event_type", payload.get("event_type", "unknown"))

        loop_event_handler = self._fastloop.loop_event_handlers.get(self.loop_name)
        if not loop_event_handler:
            return self._ok()

        loop_event = TelnyxRxMessageEvent(
            event_type=event_type,
            payload=payload,
        )

        mapped_request: dict[str, Any] = loop_event.to_dict()
        await loop_event_handler(mapped_request)

        return self._ok()

    def events(self) -> list[Any]:
        return [
            TelnyxRxMessageEvent,
            TelnyxTxMessageEvent,
        ]

    async def emit(self, event: Any) -> None:
        _event: TelnyxRxMessageEvent | TelnyxTxMessageEvent = cast(
            "TelnyxRxMessageEvent | TelnyxTxMessageEvent",
            event,
        )

        if isinstance(_event, TelnyxTxMessageEvent):
            payload: dict[str, Any] = {
                "to": _event.to,
                "text": _event.text,
                "use_profile_webhooks": _event.use_profile_webhooks,
            }

            # Handle 'from' or 'messaging_profile_id'
            from_val = _event.from_number or self.config.default_from
            profile_id_val = _event.messaging_profile_id or self.config.messaging_profile_id

            if from_val:
                payload["from"] = from_val
            
            if profile_id_val:
                payload["messaging_profile_id"] = profile_id_val

            if _event.subject:
                payload["subject"] = _event.subject

            if _event.media_urls:
                payload["media_urls"] = _event.media_urls
                payload["type"] = "MMS"
            else:
                payload["type"] = "SMS"

            if _event.webhook_url:
                payload["webhook_url"] = _event.webhook_url

            if _event.webhook_failover_url:
                payload["webhook_failover_url"] = _event.webhook_failover_url

            response = await self.client.post(
                "/messages",
                json=payload,
            )
            if response.is_error:
                logger.error(
                    "Telnyx API error",
                    extra={
                        "status_code": response.status_code,
                        "response_body": response.text,
                    },
                )
            response.raise_for_status()
