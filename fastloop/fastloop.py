import asyncio
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager
from enum import Enum
from http import HTTPStatus
from queue import Queue
from typing import Any

import hypercorn
import hypercorn.asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from hypercorn.run import run
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

from .config import ConfigManager, create_config_manager
from .constants import WATCHDOG_INTERVAL_S
from .context import LoopContext
from .exceptions import LoopAlreadyDefinedError, LoopNotFoundError
from .integrations import Integration
from .logging import configure_logging, setup_logger
from .loop import LoopEvent, LoopManager
from .state.state import LoopState, StateManager, create_state_manager
from .types import BaseConfig, LoopStatus
from .utils import get_func_import_path, import_func_from_path, infer_application_path

logger = setup_logger()


class FastLoop(FastAPI):
    def __init__(
        self,
        name: str,
        config: dict[str, Any] | None = None,
        event_types: dict[str, BaseModel] | None = None,
        *args: Any,
        **kwargs: Any,
    ):
        @asynccontextmanager
        async def lifespan(_: FastAPI):
            self._monitor_task = asyncio.create_task(
                LoopMonitor(
                    state_manager=self.state_manager,
                    loop_manager=self.loop_manager,
                    restart_callback=self.restart_loop,
                    wake_queue=self.wake_queue,
                    fastloop_instance=self,
                ).run()
            )

            yield

            self._monitor_task.cancel()
            await self.loop_manager.stop_all()

        super().__init__(*args, **kwargs, lifespan=lifespan)

        self.name = name
        self.loop_event_handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._event_types: dict[str, BaseModel] = event_types or {}
        self.config_manager: ConfigManager = create_config_manager(BaseConfig)
        self.wake_queue: Queue[str] = Queue()
        self.state_manager: StateManager = create_state_manager(
            app_name=self.name,
            config=self.config.state,
            wake_queue=self.wake_queue,
        )
        self.loop_manager: LoopManager = LoopManager(self.config, self.state_manager)
        self._monitor_task: asyncio.Task[None] | None = None
        self._loop_start_func: Callable[[LoopContext], None] | None = None
        self._loop_metadata: dict[str, dict[str, Any]] = {}

        if config:
            self.config_manager.config_data.update(config)

        configure_logging(
            pretty_print=self.config_manager.get("prettyPrintLogs", False)
        )

        cors_config = self.config_manager.get("cors", {})
        if cors_config.get("enabled", True):
            logger.info("Adding CORS middleware", extra={"cors_config": cors_config})
            self.add_middleware(
                CORSMiddleware,
                allow_origins=cors_config.get("allow_origins", ["*"]),
                allow_credentials=cors_config.get("allow_credentials", True),
                allow_methods=cors_config.get("allow_methods", ["*"]),
                allow_headers=cors_config.get("allow_headers", ["*"]),
            )

        @self.get("/events/{loop_id}/history")
        async def events_history_endpoint(loop_id: str):  # type: ignore
            events = await self.state_manager.get_event_history(loop_id)
            return [event.to_dict() for event in events]  # type: ignore

        @self.get("/events/{loop_id}/sse")
        async def events_sse_endpoint(loop_id: str):  # type: ignore
            return await self.loop_manager.events_sse(loop_id)

    @property
    def config(self) -> BaseConfig:
        return self.config_manager.get_config()

    def register_events(self, event_classes: list[type[LoopEvent]]):
        for event_class in event_classes:
            self.register_event(event_class)

    def register_event(
        self,
        event_class: type[LoopEvent],
    ):
        if not hasattr(event_class, "type"):
            event_type = event_class.model_fields["type"].default
            event_class.type = event_type
        else:
            event_type = event_class.type

        if not event_type or event_type == "" or event_type == PydanticUndefined:
            raise ValueError(
                f"You must set the 'type' class attribute or a 'type' field with a default value on the event class: {event_class.__name__}"
            )

        if event_type in self._event_types:
            logger.warning(
                f"Event type '{event_type}' is already registered. Overwriting.",
                extra={"event_type": event_type, "event_class": event_class.__name__},
            )

        self._event_types[event_type] = event_class  # type: ignore

    def run(
        self,
        host: str = "0.0.0.0",
        port: int = 8000,
        debug: bool = False,
    ):
        config_host = self.config_manager.get("host", host)
        config_port = self.config_manager.get("port", port)
        config_debug = self.config_manager.get("debugMode", debug)
        shutdown_timeout = self.config_manager.get("shutdownTimeoutS", 10)

        config = hypercorn.config.Config()
        config.bind = [f"{config_host}:{config_port}"]
        config.worker_class = "asyncio"
        config.graceful_timeout = shutdown_timeout

        if debug:
            config_debug = debug

        config.debug = config_debug
        if config.debug:
            config.use_reloader = True

            if not hasattr(config, "application_path"):
                config.application_path = infer_application_path(self)

        if not hasattr(config, "application_path"):
            asyncio.run(hypercorn.asyncio.serve(self, config))
            return

        run(config)

    def loop(
        self,
        name: str,
        start_event: str | Enum | type[LoopEvent] | None = None,
        on_start: Callable[..., Any] | None = None,
        on_stop: Callable[..., Any] | None = None,
        integrations: list[Integration] | None = None,
        stop_on_disconnect: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def _decorator(
            func: Callable[..., Any],
        ) -> Callable[..., Any]:
            for integration in integrations or []:
                logger.info(
                    f"Registering integration: {integration.type()}",
                    extra={"type": integration.type(), "loop_name": name},
                )
                integration.register(self, name)

            start_event_key = None
            if start_event:
                if (
                    start_event
                    and isinstance(start_event, type)
                    and issubclass(start_event, LoopEvent)  # type: ignore
                ):
                    start_event_key = start_event.type
                elif hasattr(start_event, "value"):
                    start_event_key = start_event.value  # type: ignore
                else:
                    start_event_key = start_event

            if name not in self._loop_metadata:
                self._loop_metadata[name] = {
                    "func": func,
                    "loop_name": name,
                    "start_event": start_event_key,
                    "on_start": on_start,
                    "on_stop": on_stop,
                    "loop_delay": self.config.loop_delay_s,
                    "integrations": integrations,
                    "stop_on_disconnect": stop_on_disconnect,
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

            async def _event_handler(request: dict[str, Any], func: Any = func):
                event_type: str | None = request.get("type")
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
                    event: LoopEvent = event_model.model_validate(request)  # type: ignore
                except ValidationError as exc:
                    errors: list[str] = []
                    for error in exc.errors():
                        field = ".".join(str(loc) for loc in error["loc"])
                        msg = error["msg"]
                        errors.append(f"{field}: {msg}")

                    raise HTTPException(
                        status_code=HTTPStatus.BAD_REQUEST,
                        detail={"message": "Invalid event data", "errors": errors},
                    ) from exc

                # Only validate against start event if this is a new loop
                # (no loop_id was passed in the event payload) and a start event was provided
                if not event.loop_id and (
                    event_type != start_event_key and start_event_key
                ):
                    raise HTTPException(
                        status_code=HTTPStatus.BAD_REQUEST,
                        detail=f"Expected start event type '{start_event_key}', got '{event_type}'",
                    )

                try:
                    loop, created = await self.state_manager.get_or_create_loop(
                        loop_name=name,
                        loop_id=event.loop_id,
                        current_function_path=get_func_import_path(func),
                    )
                    if created:
                        logger.info(
                            "Created new loop",
                            extra={
                                "loop_id": loop.loop_id,
                            },
                        )
                    else:
                        func = import_func_from_path(loop.current_function_path)

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
                    integrations=self._loop_metadata[name].get("integrations", []),
                )

                await self.state_manager.push_event(loop.loop_id, event)

                if loop.status != LoopStatus.RUNNING:
                    loop = await self.state_manager.update_loop_status(
                        loop.loop_id, LoopStatus.RUNNING
                    )

                func_to_run: Any = func  # default to the local func
                if not created:
                    func_to_run = import_func_from_path(loop.current_function_path)

                started = await self.loop_manager.start(
                    func=func_to_run,
                    loop_start_func=on_start,
                    loop_stop_func=on_stop,
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
                    await self.state_manager.update_loop_status(
                        loop_id, LoopStatus.STOPPED
                    )
                    return JSONResponse(
                        content={"message": "Loop stopped"},
                        media_type="application/json",
                        status_code=HTTPStatus.OK,
                    )
                except LoopNotFoundError as e:
                    raise HTTPException(
                        status_code=HTTPStatus.NOT_FOUND,
                        detail=f"Loop {loop_id} not found",
                    ) from e

            async def _pause_handler(loop_id: str):
                try:
                    await self.state_manager.update_loop_status(
                        loop_id, LoopStatus.IDLE
                    )
                    return JSONResponse(
                        content={"message": "Loop paused"},
                        media_type="application/json",
                        status_code=HTTPStatus.OK,
                    )
                except LoopNotFoundError as e:
                    raise HTTPException(
                        status_code=HTTPStatus.NOT_FOUND,
                        detail=f"Loop {loop_id} not found",
                    ) from e

            # Register loop endpoints
            self.add_api_route(
                path=f"/{name}",
                endpoint=_event_handler,
                methods=["POST"],
                response_model=None,
            )
            self.loop_event_handlers[name] = _event_handler

            self.add_api_route(
                path=f"/{name}",
                endpoint=_list_events_handler,
                methods=["GET"],
                response_model=None,
            )

            self.add_api_route(
                path=f"/{name}/{{loop_id}}",
                endpoint=_retrieve_handler,
                methods=["GET"],
                response_model=None,
            )

            self.add_api_route(
                path=f"/{name}/{{loop_id}}/stop",
                endpoint=_stop_handler,
                methods=["POST"],
                response_model=None,
            )

            self.add_api_route(
                path=f"/{name}/{{loop_id}}/pause",
                endpoint=_pause_handler,
                methods=["POST"],
                response_model=None,
            )

            return func

        return _decorator

    def event(self, event_type: str) -> Callable[[type[LoopEvent]], type[LoopEvent]]:
        def _decorator(cls: type[LoopEvent]) -> type[LoopEvent]:
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
                integrations=metadata.get("integrations", []),
            )

            func = import_func_from_path(loop.current_function_path)
            started = await self.loop_manager.start(
                func=func,
                loop_start_func=metadata.get("on_start"),
                loop_stop_func=metadata.get("on_stop"),
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

        except BaseException as e:
            logger.error(
                "Failed to restart loop",
                extra={
                    "loop_id": loop.loop_id,  # type: ignore
                    "error": str(e),
                },
            )
            return False

    async def has_active_clients(self, loop_id: str) -> bool:
        """Check if a loop has any active SSE client connections"""
        client_count = await self.state_manager.get_active_client_count(loop_id)
        return client_count > 0


class LoopMonitor:
    def __init__(
        self,
        state_manager: StateManager,
        loop_manager: LoopManager,
        restart_callback: Callable[[str], Coroutine[Any, Any, bool]],
        wake_queue: Queue[str],
        fastloop_instance: FastLoop,
    ):
        self.state_manager: StateManager = state_manager
        self.loop_manager: LoopManager = loop_manager
        self.restart_callback: Callable[[str], Coroutine[Any, Any, bool]] = (
            restart_callback
        )
        self.wake_queue: Queue[str] = wake_queue
        self.fastloop_instance: FastLoop = fastloop_instance
        self._stop_event: asyncio.Event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self):
        while not self._stop_event.is_set():
            try:
                if not self.wake_queue.empty():
                    loop_id = self.wake_queue.get()
                    if await self.state_manager.has_claim(loop_id):
                        continue

                    logger.info(
                        "Loop woke up, restarting",
                        extra={"loop_id": loop_id},
                    )
                    if not await self.restart_callback(loop_id):
                        await self.state_manager.update_loop_status(
                            loop_id, LoopStatus.STOPPED
                        )
                        await self.loop_manager.stop(loop_id)

                    continue

                loop_ids: set[str] = await self.state_manager.get_all_loop_ids()
                active_loop_ids: set[str] = await self.loop_manager.active_loop_ids()
                loops_running: set[str] = active_loop_ids.intersection(loop_ids)

                for loop_id in loops_running:
                    loop = await self.state_manager.get_loop(loop_id)

                    if (
                        loop.status in LoopStatus.IDLE
                        or loop.status == LoopStatus.STOPPED
                    ):
                        if await self.state_manager.has_claim(loop_id):
                            continue

                        logger.info(
                            "Loop is idle or stopped, stopping",
                            extra={"loop_id": loop_id},
                        )

                        await self.loop_manager.stop(loop_id)
                        continue

                loops: list[LoopState] = await self.state_manager.get_all_loops(
                    status=LoopStatus.RUNNING
                )
                for loop in loops:
                    # Restart loop if it has no claim and is not idle (maybe the task crashed or was interrupted)
                    if not await self.state_manager.has_claim(loop.loop_id):
                        logger.info(
                            "Loop has no claim, restarting",
                            extra={
                                "loop_id": loop.loop_id,
                            },
                        )

                        if not await self.restart_callback(loop.loop_id):
                            await self.state_manager.update_loop_status(
                                loop.loop_id, LoopStatus.STOPPED
                            )
                            await self.loop_manager.stop(loop.loop_id)

                        continue

                # Check for loops with stop_on_disconnect=true that have no active clients
                for loop in loops:
                    if (
                        loop.loop_name
                        and loop.loop_name in self.fastloop_instance._loop_metadata
                    ):
                        metadata = self.fastloop_instance._loop_metadata[loop.loop_name]
                        if metadata.get("stop_on_disconnect", False):
                            has_clients = (
                                await self.fastloop_instance.has_active_clients(
                                    loop.loop_id
                                )
                            )
                            if not has_clients:
                                logger.info(
                                    "Loop has stop_on_disconnect=true and no active clients, stopping",
                                    extra={
                                        "loop_id": loop.loop_id,
                                        "loop_name": loop.loop_name,
                                    },
                                )
                                await self.state_manager.update_loop_status(
                                    loop.loop_id, LoopStatus.STOPPED
                                )
                                await self.loop_manager.stop(loop.loop_id)

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
