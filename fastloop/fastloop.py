import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from http import HTTPStatus

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from .config import ConfigManager, create_config_manager
from .constants import WATCHDOG_INTERVAL_S
from .context import LoopContext
from .exceptions import LoopAlreadyDefinedError, LoopNotFoundError
from .logging import configure_logging, setup_logger
from .loop import LoopEvent, LoopManager
from .state.state import LoopState, StateManager, create_state_manager
from .types import BaseConfig, LoopStatus

logger = setup_logger(__name__)


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
        self.state_manager: StateManager = create_state_manager(
            self.name, self.config.state
        )
        self.loop_manager: LoopManager = LoopManager(self.config, self.state_manager)
        self._monitor_task: asyncio.Task | None = None
        self._loop_start_func: Callable | None = None
        self._loop_metadata: dict[str, dict] = {}

        if config:
            self.config_manager.config_data.update(config)

        configure_logging(
            pretty_print=self.config_manager.get("prettyPrintLogs", False)
        )

        @asynccontextmanager
        async def lifespan(_: FastAPI):
            self._monitor_task = asyncio.create_task(
                LoopMonitor(
                    state_manager=self.state_manager,
                    loop_manager=self.loop_manager,
                    restart_callback=self.restart_loop,
                ).run()
            )
            yield
            self._monitor_task.cancel()
            await self.loop_manager.stop_all()

        self._app: FastAPI = FastAPI(lifespan=lifespan)
        self._app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @self._app.get("/events/{loop_id}/history")
        async def events_history_endpoint(loop_id: str):
            events = await self.state_manager.get_event_history(loop_id)
            return [event.to_dict() for event in events]

        @self._app.get("/events/{loop_id}/sse")
        async def events_sse_endpoint(loop_id: str):
            return await self.loop_manager.events_sse(loop_id)

    @property
    def config(self) -> BaseConfig:
        return self.config_manager.get_config()

    def register_event(self, event_type: LoopEvent):
        self._event_types[event_type.type] = event_type

    def run(self, host: str = "0.0.0.0", port: int = 8000):
        config_host = self.config_manager.get("host", host)
        config_port = self.config_manager.get("port", port)
        uvicorn.run(self._app, host=config_host, port=config_port, log_config=None)

    def loop(
        self,
        name: str,
        start_event: str | Enum | type[LoopEvent],
        idle_timeout: float = 600.0,
        on_loop_start: Callable | None = None,
    ) -> Callable:
        def _decorator(func: Callable) -> Callable:
            if isinstance(start_event, type) and issubclass(start_event, LoopEvent):
                start_event_key = start_event.type
            elif hasattr(start_event, "value"):
                start_event_key = start_event.value
            else:
                start_event_key = start_event

            if name not in self._loop_metadata:
                self._loop_metadata[name] = {
                    "func": func,
                    "loop_name": name,
                    "start_event": start_event_key,
                    "idle_timeout": idle_timeout,
                    "on_loop_start": on_loop_start,
                    "loop_delay": self.config.loop_delay_s,
                }
            else:
                raise LoopAlreadyDefinedError(f"Loop {name} already registered")

            async def _list_events_handler():
                logger.info(
                    "Listing loop event types",
                    extra={"event_types": list(self._event_types.keys())},
                )
                return JSONResponse(
                    content={
                        name: model.model_json_schema()
                        for name, model in self._event_types.items()
                    },
                    media_type="application/json",
                )

            async def _event_handler(request: dict):
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

                # Only validate against start event if this is a new loop
                # (no loop_id was passed in the event payload)
                if not event.loop_id and event_type != start_event_key:
                    raise HTTPException(
                        status_code=HTTPStatus.BAD_REQUEST,
                        detail=f"Expected start event type '{start_event_key}', got '{event_type}'",
                    )

                try:
                    loop, created = await self.state_manager.get_or_create_loop(
                        loop_name=name,
                        loop_id=event.loop_id,
                        idle_timeout=idle_timeout,
                    )
                    if created:
                        logger.info(
                            "Created new loop",
                            extra={
                                "loop_id": loop.loop_id,
                            },
                        )
                except LoopNotFoundError as e:
                    raise HTTPException(
                        status_code=HTTPStatus.NOT_FOUND,
                        detail=f"Loop {event.loop_id} not found",
                    ) from e

                # If a loop was previously stopped, we don't want to start it again
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

                started = await self.loop_manager.start(
                    func=func,
                    loop_start_func=on_loop_start,
                    context=context,
                    loop=loop,
                    loop_delay=self.config.loop_delay_s,
                )
                if started:
                    logger.info(
                        "Loop started",
                        extra={
                            "loop_id": loop.loop_id,
                        },
                    )
                    loop = await self.state_manager.update_loop_status(
                        loop.loop_id, LoopStatus.RUNNING
                    )
                else:
                    loop = await self.state_manager.get_loop(loop.loop_id)

                return loop

            async def _retrieve_handler(loop_id: str):
                try:
                    loop = await self.state_manager.get_loop(loop_id)
                except LoopNotFoundError as e:
                    raise HTTPException(
                        status_code=HTTPStatus.NOT_FOUND,
                        detail=f"Loop {loop_id} not found",
                    ) from e

                return JSONResponse(
                    content=loop.to_json(), media_type="application/json"
                )

            async def _stop_handler(loop_id: str):
                try:
                    await self.loop_manager.update_loop_status(
                        loop_id, LoopStatus.STOPPED
                    )
                except LoopNotFoundError as e:
                    raise HTTPException(
                        status_code=HTTPStatus.NOT_FOUND,
                        detail=f"Loop {loop_id} not found",
                    ) from e

            async def _pause_handler(loop_id: str):
                try:
                    await self.loop_manager.update_loop_status(loop_id, LoopStatus.IDLE)
                except LoopNotFoundError as e:
                    raise HTTPException(
                        status_code=HTTPStatus.NOT_FOUND,
                        detail=f"Loop {loop_id} not found",
                    ) from e

            # Register loop endpoints
            self._app.add_api_route(
                path=f"/{name}",
                endpoint=_event_handler,
                methods=["POST"],
                response_model=None,
            )

            self._app.add_api_route(
                path=f"/{name}",
                endpoint=_list_events_handler,
                methods=["GET"],
                response_model=None,
            )

            self._app.add_api_route(
                path=f"/{name}/{{loop_id}}",
                endpoint=_retrieve_handler,
                methods=["GET"],
                response_model=None,
            )

            self._app.add_api_route(
                path=f"/{name}/{{loop_id}}/stop",
                endpoint=_stop_handler,
                methods=["POST"],
                response_model=None,
            )

            self._app.add_api_route(
                path=f"/{name}/{{loop_id}}/pause",
                endpoint=_pause_handler,
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

    async def restart_loop(self, loop_id: str) -> bool:
        """Restart a loop using stored metadata (keyed by loop name)"""

        try:
            loop = await self.state_manager.get_loop(loop_id)
            loop_name = loop.loop_name

            if not loop_name or loop_name not in self._loop_metadata:
                logger.warning(
                    "No metadata found for loop",
                    extra={"loop_name": loop_name, "loop_id": loop_id},
                )
                return False

            metadata = self._loop_metadata[loop_name]
            initial_event = await self.state_manager.get_initial_event(loop_id)
            context = LoopContext(
                loop_id=loop.loop_id,
                initial_event=initial_event,
                state_manager=self.state_manager,
            )

            started = await self.loop_manager.start(
                func=metadata["func"],
                loop_start_func=metadata["on_loop_start"],
                context=context,
                loop=loop,
                loop_delay=metadata["loop_delay"],
            )
            if started:
                await self.state_manager.update_loop_status(
                    loop.loop_id, LoopStatus.RUNNING
                )
                logger.info(
                    "Restarted loop",
                    extra={
                        "loop_id": loop.loop_id,
                    },
                )
                return True
            else:
                logger.warning(
                    "Failed to restart loop",
                    extra={
                        "loop_id": loop.loop_id,
                    },
                )
                return False

        except Exception as e:
            logger.error(
                "Failed to restart loop",
                extra={
                    "loop_id": loop.loop_id,
                    "error": str(e),
                },
            )
            return False


class LoopMonitor:
    def __init__(
        self,
        state_manager: StateManager,
        loop_manager: LoopManager,
        restart_callback: Callable,
    ):
        self.state_manager: StateManager = state_manager
        self.loop_manager: LoopManager = loop_manager
        self.restart_callback: Callable = restart_callback
        self._stop_event = asyncio.Event()

    def stop(self):
        self._stop_event.set()

    async def run(self):
        while not self._stop_event.is_set():
            try:
                loop_ids: set[str] = await self.state_manager.get_all_loop_ids()
                active_loop_ids: set[str] = await self.loop_manager.active_loop_ids()
                loops_running: set[str] = active_loop_ids.intersection(loop_ids)

                for loop_id in loops_running:
                    loop = await self.state_manager.get_loop(loop_id)

                    if (
                        loop.status in LoopStatus.IDLE
                        or loop.status == LoopStatus.STOPPED
                    ):
                        await self.loop_manager.stop(loop_id)
                        continue

                    if loop.last_event_at + loop.idle_timeout < int(
                        datetime.now().timestamp()
                    ):
                        logger.info(
                            "Loop is idle, pausing",
                            extra={
                                "loop_id": loop.loop_id,
                            },
                        )
                        await self.loop_manager.stop(loop.loop_id)
                        continue

                loops: list[LoopState] = await self.state_manager.get_all_loops(
                    status=LoopStatus.RUNNING
                )
                for loop in loops:
                    exceeded_idle_timeout = (
                        loop.last_event_at + loop.idle_timeout
                        < int(datetime.now().timestamp())
                    )

                    # Restart loop if it has no claim and is not idle (maybe the task crashed or was interrupted)
                    if (
                        not await self.state_manager.has_claim(loop.loop_id)
                        and not exceeded_idle_timeout
                    ):
                        logger.info(
                            "Loop has no claim, restarting",
                            extra={
                                "loop_id": loop.loop_id,
                            },
                        )
                        await self.restart_callback(loop.loop_id)
                        continue

                    # Pause loop if it has exceeded the idle timeout
                    elif exceeded_idle_timeout:
                        logger.info(
                            "Loop exceeded idle timeout, pausing",
                            extra={
                                "loop_id": loop.loop_id,
                                "last_event_at": loop.last_event_at,
                                "idle_timeout": loop.idle_timeout,
                            },
                        )
                        await self.state_manager.update_loop_status(
                            loop.loop_id, LoopStatus.IDLE
                        )
                        continue

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
                logger.error(
                    "Error in loop monitor",
                    extra={"error": str(e)},
                )
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
