import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from http import HTTPStatus

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ValidationError

from .config import ConfigManager, create_config_manager
from .constants import WATCHDOG_INTERVAL_S
from .context import LoopContext
from .loop import LoopEvent, LoopManager, LoopState
from .state.state import StateManager, create_state_manager
from .types import BaseConfig, LoopStatus

logger = logging.getLogger(__name__)


class FastLoop:
    def __init__(
        self,
        name: str,
        config: dict | None = None,
        event_types: dict[str, BaseModel] | None = None,
    ):
        self.name = name
        self._event_types: dict[str, BaseModel] = event_types or {}
        self.config_manager: ConfigManager = create_config_manager(BaseConfig)
        self.state_manager: StateManager = create_state_manager(self.config.state)
        self.loop_manager: LoopManager = LoopManager(self.config, self.state_manager)
        self._monitor_task: asyncio.Task | None = None
        self._loop_start_func: Callable | None = None

        if config:
            self.config_manager.config_data.update(config)

        @asynccontextmanager
        async def lifespan(_: FastAPI):
            self._monitor_task = asyncio.create_task(
                LoopMonitor(self.state_manager, self.loop_manager).run()
            )
            yield
            self._monitor_task.cancel()
            await self.loop_manager.stop_all()

        self._app: FastAPI = FastAPI(lifespan=lifespan)

        @self._app.get("/events/{loop_id}/{event_type}")
        async def events_sse_endpoint(loop_id: str, event_type: str):
            return await self.loop_manager.events(loop_id, event_type)

        @self._app.get("/events/{loop_id}/history")
        async def events_history_endpoint(loop_id: str):
            events = await self.state_manager.get_event_history(loop_id)
            return [event.to_dict() for event in events]

    @property
    def config(self) -> BaseConfig:
        return self.config_manager.get_config()

    def register_event(self, event_type: LoopEvent):
        self._event_types[event_type.type] = event_type

    def run(self, host: str = "0.0.0.0", port: int = 8000):
        config_host = self.config_manager.get("host", host)
        config_port = self.config_manager.get("port", port)
        uvicorn.run(self._app, host=config_host, port=config_port)

    def loop(
        self,
        name: str,
        start_event: str | Enum | type[LoopEvent],
        idle_timeout: float = 60.0,
        on_loop_start: Callable | None = None,
    ) -> Callable:
        def _decorator(func: Callable) -> Callable:
            if isinstance(start_event, type) and issubclass(start_event, LoopEvent):
                start_event_key = start_event.type
            elif hasattr(start_event, "value"):
                start_event_key = start_event.value
            else:
                start_event_key = start_event

            async def _route_handler(request: dict):
                event_type = request.get("type")
                if not event_type:
                    raise HTTPException(
                        status_code=HTTPStatus.BAD_REQUEST,
                        detail="Event type is required",
                    )

                if event_type not in self._event_types:
                    raise HTTPException(
                        status_code=HTTPStatus.BAD_REQUEST,
                        detail=f"Unknown event type: {event_type}",
                    )

                event_model = self._event_types[event_type]

                try:
                    event = event_model.model_validate(request)
                except ValidationError as exc:
                    errors = []
                    for error in exc.errors():
                        field = ".".join(str(loc) for loc in error["loc"])
                        msg = error["msg"]
                        errors.append(f"{field}: {msg}")

                    raise HTTPException(
                        status_code=HTTPStatus.BAD_REQUEST,
                        detail={"message": "Invalid event data", "errors": errors},
                    ) from exc

                # Only validate against start event if this is a new loop (no loop_id)
                if not event.loop_id and event_type != start_event_key:
                    raise HTTPException(
                        status_code=HTTPStatus.BAD_REQUEST,
                        detail=f"Expected start event type '{start_event_key}', got '{event_type}'",
                    )

                loop, created = await self.state_manager.get_or_create_loop(
                    loop_name=name,
                    loop_id=event.loop_id,
                    idle_timeout=idle_timeout,
                )
                if created:
                    logger.debug(f"Created new loop: {loop.loop_id}")
                else:
                    logger.debug(f"Reused existing loop: {loop.loop_id}")

                # If a loop was explicitly stopped, we don't want to start it again
                if loop.status == LoopStatus.STOPPED:
                    raise HTTPException(
                        status_code=HTTPStatus.BAD_REQUEST,
                        detail=f"Loop {loop.loop_id} is stopped",
                    )

                event.loop_id = loop.loop_id
                context = LoopContext(
                    loop_id=loop.loop_id,
                    initial_event=event,
                    state_manager=self.state_manager,
                )

                await self.state_manager.push_event(loop.loop_id, event)
                await self.loop_manager.start(
                    func=func,
                    loop_start_func=on_loop_start,
                    context=context,
                    loop=loop,
                    loop_delay=self.config.loop_delay_s,
                )
                return loop

            self._app.add_api_route(
                path=f"/{name}",
                endpoint=_route_handler,
                methods=["POST"],
                response_model=None,
            )
            return func

        return _decorator

    def event(self, event_type: str):
        def _decorator(cls):
            cls.type = event_type
            self.register_event(cls)
            return cls

        return _decorator


class LoopMonitor:
    def __init__(self, state_manager: StateManager, loop_manager: LoopManager):
        self.state_manager: StateManager = state_manager
        self.loop_manager: LoopManager = loop_manager
        self._stop_event = asyncio.Event()

    def stop(self):
        self._stop_event.set()

    async def run(self):
        while not self._stop_event.is_set():
            try:
                loops: list[LoopState] = await self.state_manager.get_all_loops(
                    status=LoopStatus.RUNNING
                )

                for loop in loops:
                    if loop.last_event_at + loop.idle_timeout < int(
                        datetime.now().timestamp()
                    ):
                        # logger.info(f"Loop {loop.loop_id} is idle, pausing")
                        pass
                        # paused = await self.loop_manager.pause(loop.loop_id)
                        # if not paused:
                        #     try:
                        #         async with self.state_manager.with_claim(loop.loop_id):
                        #             loop.status = LoopStatus.IDLE
                        #             await self.state_manager.update_loop(loop.loop_id, loop)
                        #             logger.info(f"Loop {loop.loop_id} paused")
                        #     except LoopClaimError as e:
                        #         logger.error(f"Error pausing loop {loop.loop_id}: {e}")

                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=WATCHDOG_INTERVAL_S
                    )
                    break
                except TimeoutError:
                    continue

            except asyncio.CancelledError:
                break
            except BaseException as e:
                logger.error(f"Error in loop monitor: {e}")
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
