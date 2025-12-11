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
from .exceptions import (
    LoopAlreadyDefinedError,
    LoopNotFoundError,
    WorkflowNotFoundError,
)
from .integrations import Integration
from .logging import configure_logging, setup_logger
from .loop import Loop, LoopEvent, LoopManager, Workflow, WorkflowManager
from .state.state import StateManager, create_state_manager
from .types import BaseConfig, LoopStatus, RetryPolicy
from .utils import get_func_import_path, import_func_from_path, infer_application_path

logger = setup_logger()


def _resolve_event_key(event: str | Enum | type[LoopEvent] | None) -> str | None:
    """Convert event type/enum/string to string key."""
    if not event:
        return None
    if isinstance(event, type) and issubclass(event, LoopEvent):
        return event.type
    if hasattr(event, "value"):
        return event.value  # type: ignore
    return event  # type: ignore


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
            await self.workflow_manager.stop_all()

        super().__init__(*args, **kwargs, lifespan=lifespan)

        self.name = name
        self.loop_event_handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._event_types: dict[str, BaseModel] = event_types or {}
        self.config_manager: ConfigManager = create_config_manager(BaseConfig)

        if config:
            self.config_manager.config_data.update(config)

        self.wake_queue: Queue[str] = Queue()
        self.state_manager: StateManager = create_state_manager(
            app_name=self.name,
            config=self.config.state,
            wake_queue=self.wake_queue,
        )
        self.loop_manager: LoopManager = LoopManager(self.config, self.state_manager)
        self.workflow_manager: WorkflowManager = WorkflowManager(self.state_manager)
        self._monitor_task: asyncio.Task[None] | None = None
        self._loop_start_func: Callable[[LoopContext], None] | None = None
        self._loop_metadata: dict[str, dict[str, Any]] = {}
        self._workflow_metadata: dict[str, dict[str, Any]] = {}

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

        @self.get("/events/{entity_id}/history")
        async def events_history_endpoint(entity_id: str):  # type: ignore
            events = await self.state_manager.get_event_history(entity_id)
            return events

        @self.get("/events/{entity_id}/sse")
        async def events_sse_endpoint(entity_id: str):  # type: ignore
            return await self.loop_manager.events_sse(entity_id)

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
        host: str | None = None,
        port: int | None = None,
        debug: bool | None = None,
    ):
        host = host if host is not None else self.config_manager.get("host", "0.0.0.0")
        port = port if port is not None else self.config_manager.get("port", 8000)
        debug = (
            debug if debug is not None else self.config_manager.get("debugMode", False)
        )
        shutdown_timeout = self.config_manager.get("shutdownTimeoutS", 10)

        config = hypercorn.config.Config()
        config.bind = [f"{host}:{port}"]
        config.worker_class = "asyncio"
        config.graceful_timeout = shutdown_timeout
        config.debug = debug

        # For debug/reload mode, we need an application path for hypercorn to reload
        application_path = None
        if config.debug:
            config.use_reloader = True
            application_path = infer_application_path(self)
            if application_path:
                config.application_path = application_path

        # Use direct serve if no valid application_path (works without reload)
        if not application_path:
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
    ) -> Callable[[Callable[..., Any] | type[Loop]], Callable[..., Any] | type[Loop]]:
        def _decorator(
            func_or_class: Callable[..., Any] | type[Loop],
        ) -> Callable[..., Any] | type[Loop]:
            is_class_based = isinstance(func_or_class, type) and issubclass(
                func_or_class, Loop
            )

            if is_class_based:
                loop_instance: Loop = func_or_class()
                func = loop_instance.loop
                loop_on_start = loop_instance.on_start
                loop_on_stop = loop_instance.on_stop
            else:
                loop_instance = None  # type: ignore
                func = func_or_class  # type: ignore
                loop_on_start = on_start
                loop_on_stop = on_stop

            for integration in integrations or []:
                logger.info(
                    f"Registering integration: {integration.type()}",
                    extra={"type": integration.type(), "loop_name": name},
                )
                integration.register(self, name)

            start_event_key = _resolve_event_key(start_event)

            if name not in self._loop_metadata:
                self._loop_metadata[name] = {
                    "func": func,
                    "loop_name": name,
                    "start_event": start_event_key,
                    "on_start": loop_on_start,
                    "on_stop": loop_on_stop,
                    "loop_delay": self.config.loop_delay_s,
                    "integrations": integrations,
                    "stop_on_disconnect": stop_on_disconnect,
                    "loop_instance": loop_instance,
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

                if loop_instance:
                    loop_instance.ctx = context
                    await loop_instance.on_event(context, event)

                if loop.status != LoopStatus.RUNNING:
                    loop = await self.state_manager.update_loop_status(
                        loop.loop_id, LoopStatus.RUNNING
                    )

                if loop_instance or created:
                    func_to_run = func
                else:
                    func_to_run = import_func_from_path(loop.current_function_path)

                started = await self.loop_manager.start(
                    func=func_to_run,
                    loop_start_func=loop_on_start,
                    loop_stop_func=loop_on_stop,
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
                    content=loop.to_dict(), media_type="application/json"
                )

            async def _cancel_handler(loop_id: str):
                try:
                    await self.state_manager.update_loop_status(
                        loop_id, LoopStatus.STOPPED
                    )
                    await self.loop_manager.stop(loop_id)
                    return JSONResponse(
                        content={"message": "Loop cancelled"},
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
                path=f"/{name}/{{loop_id}}/cancel",
                endpoint=_cancel_handler,
                methods=["POST"],
                response_model=None,
            )

            self.add_api_route(
                path=f"/{name}/{{loop_id}}/pause",
                endpoint=_pause_handler,
                methods=["POST"],
                response_model=None,
            )

            return func_or_class

        return _decorator

    def event(self, event_type: str) -> Callable[[type[LoopEvent]], type[LoopEvent]]:
        def _decorator(cls: type[LoopEvent]) -> type[LoopEvent]:
            cls.type = event_type
            self.register_event(cls)
            return cls

        return _decorator

    def workflow(
        self,
        name: str,
        start_event: str | Enum | type[LoopEvent] | None = None,
        on_start: Callable[..., Any] | None = None,
        on_stop: Callable[..., Any] | None = None,
        on_block_complete: Callable[..., Any] | None = None,
        on_error: Callable[..., Any] | None = None,
        plan: Callable[..., Any] | None = None,
        retry: RetryPolicy | None = None,
    ) -> Callable[
        [Callable[..., Any] | type[Workflow]], Callable[..., Any] | type[Workflow]
    ]:
        def _decorator(
            func_or_class: Callable[..., Any] | type[Workflow],
        ) -> Callable[..., Any] | type[Workflow]:
            is_class_based = isinstance(func_or_class, type) and issubclass(
                func_or_class, Workflow
            )

            if is_class_based:
                workflow_instance: Workflow = func_or_class()
                func = workflow_instance.execute
                workflow_on_start = workflow_instance.on_start
                workflow_on_stop = workflow_instance.on_stop
                workflow_on_block_complete = workflow_instance.on_block_complete
                workflow_on_error = workflow_instance.on_error
                workflow_plan = getattr(workflow_instance, "plan", None) or plan
            else:
                workflow_instance = None  # type: ignore
                func = func_or_class  # type: ignore
                workflow_on_start = on_start
                workflow_on_stop = on_stop
                workflow_on_block_complete = on_block_complete
                workflow_on_error = on_error
                workflow_plan = plan

            start_event_key = _resolve_event_key(start_event)

            if name in self._workflow_metadata:
                raise LoopAlreadyDefinedError(f"Workflow {name} already registered")

            self._workflow_metadata[name] = {
                "func": func,
                "on_start": workflow_on_start,
                "on_stop": workflow_on_stop,
                "on_block_complete": workflow_on_block_complete,
                "on_error": workflow_on_error,
                "plan": workflow_plan,
                "workflow_instance": workflow_instance,
                "retry_policy": retry,
            }

            async def _start_handler(request: dict[str, Any]):
                event_type = request.get("type")
                blocks_raw = request.get("blocks", [])
                workflow_run_id_req = request.get("workflow_run_id")

                if start_event_key and event_type != start_event_key:
                    raise HTTPException(
                        status_code=HTTPStatus.BAD_REQUEST,
                        detail=f"Expected event type '{start_event_key}'",
                    )

                if not blocks_raw or not isinstance(blocks_raw, list):
                    raise HTTPException(
                        status_code=HTTPStatus.BAD_REQUEST,
                        detail="blocks is required and must be a list",
                    )

                for i, block in enumerate(blocks_raw):
                    if not isinstance(block, dict):
                        raise HTTPException(
                            status_code=HTTPStatus.BAD_REQUEST,
                            detail=f"blocks[{i}] must be an object",
                        )
                    if "text" not in block or "type" not in block:
                        raise HTTPException(
                            status_code=HTTPStatus.BAD_REQUEST,
                            detail=f"blocks[{i}] must have 'text' and 'type' fields",
                        )

                workflow, _ = await self.state_manager.get_or_create_workflow(
                    workflow_name=name,
                    workflow_run_id=workflow_run_id_req,
                    blocks=blocks_raw,
                )

                if workflow.status == LoopStatus.STOPPED:
                    raise HTTPException(
                        status_code=HTTPStatus.BAD_REQUEST,
                        detail=f"Workflow run {workflow.workflow_run_id} is stopped",
                    )

                context = LoopContext(
                    loop_id=workflow.workflow_run_id,
                    initial_event=None,
                    state_manager=self.state_manager,
                )

                if workflow.status != LoopStatus.RUNNING:
                    await self.state_manager.update_workflow_status(
                        workflow.workflow_run_id, LoopStatus.RUNNING
                    )

                await self.workflow_manager.start(
                    func,
                    context,
                    workflow,
                    on_start=workflow_on_start,
                    on_stop=workflow_on_stop,
                    on_block_complete=workflow_on_block_complete,
                    on_error=workflow_on_error,
                    plan=workflow_plan,
                    retry_policy=retry,
                )
                return (
                    await self.state_manager.get_workflow(workflow.workflow_run_id)
                ).to_dict()

            async def _get_handler(workflow_run_id: str):
                try:
                    workflow = await self.state_manager.get_workflow(workflow_run_id)
                    return JSONResponse(content=workflow.to_dict())
                except WorkflowNotFoundError as e:
                    raise HTTPException(
                        status_code=HTTPStatus.NOT_FOUND, detail=str(e)
                    ) from e

            async def _cancel_handler(workflow_run_id: str):
                try:
                    await self.state_manager.update_workflow_status(
                        workflow_run_id, LoopStatus.STOPPED
                    )
                    await self.state_manager.clear_workflow_wake_time(workflow_run_id)
                    await self.workflow_manager.stop(workflow_run_id)
                    return JSONResponse(content={"message": "Workflow run cancelled"})
                except WorkflowNotFoundError as e:
                    raise HTTPException(
                        status_code=HTTPStatus.NOT_FOUND, detail=str(e)
                    ) from e

            async def _event_handler(request: dict[str, Any]):
                workflow_run_id = request.get("workflow_run_id")
                event_type = request.get("type")

                if not workflow_run_id:
                    raise HTTPException(
                        status_code=HTTPStatus.BAD_REQUEST,
                        detail="workflow_run_id required",
                    )
                if not event_type or event_type not in self._event_types:
                    raise HTTPException(
                        status_code=HTTPStatus.BAD_REQUEST,
                        detail=f"Unknown event: {event_type}",
                    )

                try:
                    workflow = await self.state_manager.get_workflow(workflow_run_id)
                except WorkflowNotFoundError as e:
                    raise HTTPException(
                        status_code=HTTPStatus.NOT_FOUND, detail=str(e)
                    ) from e

                event_model = self._event_types[event_type]
                try:
                    event: LoopEvent = event_model.model_validate(request)
                except ValidationError as exc:
                    raise HTTPException(
                        status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)
                    ) from exc

                event.loop_id = workflow_run_id
                await self.state_manager.push_event(workflow_run_id, event)
                return workflow.to_dict()

            self.add_api_route(f"/{name}", _start_handler, methods=["POST"])
            self.add_api_route(
                f"/{name}/{{workflow_run_id}}", _get_handler, methods=["GET"]
            )
            self.add_api_route(
                f"/{name}/{{workflow_run_id}}/event", _event_handler, methods=["POST"]
            )
            self.add_api_route(
                f"/{name}/{{workflow_run_id}}/cancel", _cancel_handler, methods=["POST"]
            )

            return func_or_class

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

            loop_instance: Loop | None = metadata.get("loop_instance")
            if loop_instance:
                loop_instance.ctx = context
                func = loop_instance.loop
            else:
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

    async def restart_workflow(self, workflow_run_id: str) -> bool:
        """Restart a workflow from its persisted state."""
        try:
            workflow = await self.state_manager.get_workflow(workflow_run_id)
            if not workflow.workflow_name:
                return False

            if workflow.status == LoopStatus.FAILED:
                logger.info(
                    "Workflow is failed, not restarting",
                    extra={"workflow_run_id": workflow_run_id},
                )
                return False

            metadata = self._workflow_metadata.get(workflow.workflow_name)
            if not metadata:
                logger.warning(
                    "No metadata for workflow",
                    extra={
                        "workflow_run_id": workflow_run_id,
                        "name": workflow.workflow_name,
                    },
                )
                return False

            context = LoopContext(
                loop_id=workflow.workflow_run_id,
                initial_event=None,
                state_manager=self.state_manager,
            )

            started = await self.workflow_manager.start(
                metadata["func"],
                context,
                workflow,
                on_start=metadata.get("on_start"),
                on_stop=metadata.get("on_stop"),
                on_block_complete=metadata.get("on_block_complete"),
                on_error=metadata.get("on_error"),
                plan=metadata.get("plan"),
                retry_policy=metadata.get("retry_policy"),
            )

            if started:
                await self.state_manager.update_workflow_status(
                    workflow.workflow_run_id, LoopStatus.RUNNING
                )
                logger.info(
                    "Restarted workflow",
                    extra={
                        "workflow_run_id": workflow.workflow_run_id,
                        "block_index": workflow.current_block_index,
                    },
                )
            return started

        except Exception as e:
            logger.error(
                "Failed to restart workflow",
                extra={"workflow_run_id": workflow_run_id, "error": str(e)},
            )
            return False


class LoopMonitor:
    def __init__(
        self,
        state_manager: StateManager,
        loop_manager: LoopManager,
        restart_callback: Callable[[str], Coroutine[Any, Any, bool]],
        wake_queue: Queue[str],
        fastloop_instance: FastLoop,
    ):
        self.state_manager = state_manager
        self.loop_manager = loop_manager
        self.restart_callback = restart_callback
        self.wake_queue = wake_queue
        self.fastloop_instance = fastloop_instance
        self._stop_event = asyncio.Event()
        self._app_start_processed = False

    def stop(self) -> None:
        self._stop_event.set()

    async def _process_wake(self, wake_id: str) -> None:
        logger.info(
            "Processing wake from queue",
            extra={"wake_id": wake_id},
        )
        if wake_id.startswith("workflow:"):
            workflow_run_id = wake_id[9:]
            if await self.state_manager.workflow_has_claim(workflow_run_id):
                logger.info(
                    "Workflow has active claim, skipping wake",
                    extra={"workflow_run_id": workflow_run_id},
                )
                return
            logger.info(
                "Workflow woke up, attempting restart",
                extra={"workflow_run_id": workflow_run_id},
            )
            try:
                if await self.fastloop_instance.restart_workflow(workflow_run_id):
                    await self.state_manager.clear_workflow_wake_time(workflow_run_id)
                    logger.info(
                        "Workflow restarted successfully",
                        extra={"workflow_run_id": workflow_run_id},
                    )
                else:
                    await self.state_manager.clear_workflow_wake_time(workflow_run_id)
                    await self.state_manager.update_workflow_status(
                        workflow_run_id, LoopStatus.STOPPED
                    )
                    logger.warning(
                        "Workflow restart failed, marked as stopped",
                        extra={"workflow_run_id": workflow_run_id},
                    )
            except Exception as e:
                logger.error(
                    "Error restarting workflow from wake",
                    extra={"workflow_run_id": workflow_run_id, "error": str(e)},
                )
        else:
            loop_id = wake_id
            if await self.state_manager.has_claim(loop_id):
                return
            logger.info("Loop woke up, restarting", extra={"loop_id": loop_id})
            if not await self.restart_callback(loop_id):
                await self.state_manager.update_loop_status(loop_id, LoopStatus.STOPPED)

    async def _check_orphaned_loops(self) -> None:
        running_loops = await self.state_manager.get_all_loops(
            status=LoopStatus.RUNNING
        )
        for loop in running_loops:
            if await self.state_manager.has_claim(loop.loop_id):
                continue
            logger.info(
                "Loop has no claim, restarting", extra={"loop_id": loop.loop_id}
            )
            if not await self.restart_callback(loop.loop_id):
                await self.state_manager.update_loop_status(
                    loop.loop_id, LoopStatus.STOPPED
                )

    async def _check_orphaned_workflows(self) -> None:
        running_workflows = await self.state_manager.get_all_workflows(
            status=LoopStatus.RUNNING
        )
        for workflow in running_workflows:
            if await self.state_manager.workflow_has_claim(workflow.workflow_run_id):
                continue
            logger.info(
                "Workflow has no claim, restarting",
                extra={
                    "workflow_run_id": workflow.workflow_run_id,
                    "block_index": workflow.current_block_index,
                },
            )
            if not await self.fastloop_instance.restart_workflow(
                workflow.workflow_run_id
            ):
                await self.state_manager.update_workflow_status(
                    workflow.workflow_run_id, LoopStatus.STOPPED
                )

    async def _check_scheduled_workflows(self) -> None:
        """Check for IDLE workflows with past-due scheduled wake times.

        This is a backup mechanism that catches workflows that may have been
        removed from the ZSET but not yet processed (e.g., if the wake queue
        consumer failed or the wake monitoring thread died).
        """
        import time

        now = time.time()
        idle_workflows = await self.state_manager.get_all_workflows(
            status=LoopStatus.IDLE
        )
        for workflow in idle_workflows:
            if not workflow.scheduled_wake_time:
                continue
            if workflow.scheduled_wake_time > now:
                continue
            if await self.state_manager.workflow_has_claim(workflow.workflow_run_id):
                continue
            claimed_from_zset = await self.state_manager.try_claim_workflow_wake(
                workflow.workflow_run_id
            )
            logger.info(
                "IDLE workflow has past-due wake time, restarting",
                extra={
                    "workflow_run_id": workflow.workflow_run_id,
                    "scheduled_wake_time": workflow.scheduled_wake_time,
                    "block_index": workflow.current_block_index,
                    "claimed_from_zset": claimed_from_zset,
                },
            )
            if await self.fastloop_instance.restart_workflow(workflow.workflow_run_id):
                await self.state_manager.clear_workflow_wake_time(
                    workflow.workflow_run_id
                )
            else:
                await self.state_manager.clear_workflow_wake_time(
                    workflow.workflow_run_id
                )
                await self.state_manager.update_workflow_status(
                    workflow.workflow_run_id, LoopStatus.STOPPED
                )

    async def _check_disconnect_stops(self) -> None:
        active_ids = await self.loop_manager.active_loop_ids()
        for loop_id in active_ids:
            try:
                loop = await self.state_manager.get_loop(loop_id)
            except LoopNotFoundError:
                continue
            if not loop.loop_name:
                continue
            metadata = self.fastloop_instance._loop_metadata.get(loop.loop_name)
            if not metadata or not metadata.get("stop_on_disconnect"):
                continue
            if not await self.fastloop_instance.has_active_clients(loop_id):
                logger.info(
                    "Loop has no clients, stopping",
                    extra={"loop_id": loop_id, "loop_name": loop.loop_name},
                )
                await self.state_manager.update_loop_status(loop_id, LoopStatus.STOPPED)
                await self.loop_manager.stop(loop_id)

    async def _process_app_start_callbacks(self) -> None:
        """Call on_app_start for each non-stopped class-based loop instance."""
        all_loops = await self.state_manager.get_all_loops()

        for loop in all_loops:
            if loop.status == LoopStatus.STOPPED or not loop.loop_name:
                continue

            metadata = self.fastloop_instance._loop_metadata.get(loop.loop_name)
            loop_instance: Loop | None = (
                metadata.get("loop_instance") if metadata else None
            )
            if not loop_instance:
                continue

            if not await self.state_manager.try_acquire_app_start_lock(loop.loop_id):
                continue

            try:
                context = LoopContext(
                    loop_id=loop.loop_id,
                    initial_event=await self.state_manager.get_initial_event(
                        loop.loop_id
                    ),
                    state_manager=self.state_manager,
                    integrations=metadata.get("integrations", []),  # type: ignore
                )
                loop_instance.ctx = context

                if await loop_instance.on_app_start(context):
                    logger.info(
                        "on_app_start returned True, starting loop",
                        extra={"loop_id": loop.loop_id, "loop_name": loop.loop_name},
                    )
                    if not await self.restart_callback(loop.loop_id):
                        await self.state_manager.update_loop_status(
                            loop.loop_id, LoopStatus.STOPPED
                        )
            except Exception as e:
                logger.error(
                    "Error in on_app_start",
                    extra={"loop_id": loop.loop_id, "error": str(e)},
                )
            finally:
                await self.state_manager.release_app_start_lock(loop.loop_id)

    async def run(self):
        from queue import Empty

        if not self._app_start_processed:
            await self._process_app_start_callbacks()
            self._app_start_processed = True

        while not self._stop_event.is_set():
            try:
                # Process all pending wakes, handling errors individually
                # Use get_nowait in a try/except to avoid race between empty() and get()
                wakes_processed = 0
                while True:
                    try:
                        wake_id = self.wake_queue.get_nowait()
                        wakes_processed += 1
                        try:
                            await self._process_wake(wake_id)
                        except Exception as e:
                            logger.error(
                                "Error processing wake",
                                extra={"wake_id": wake_id, "error": str(e)},
                            )
                    except Empty:
                        break

                await self._check_orphaned_loops()
                await self._check_orphaned_workflows()
                await self._check_scheduled_workflows()
                await self._check_disconnect_stops()

                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=WATCHDOG_INTERVAL_S
                    )
                    break
                except TimeoutError:
                    pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in monitor", extra={"error": str(e)})
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
