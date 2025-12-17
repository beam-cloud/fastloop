"""
FastLoop application class.

This module contains the main FastLoop class that provides:
- HTTP server with FastAPI
- Loop and Workflow decorators
- Event registration
"""

import asyncio
from collections.abc import Callable
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
from .context import LoopContext
from .exceptions import (
    LoopAlreadyDefinedError,
    LoopNotFoundError,
    TaskNotFoundError,
    WorkflowNotFoundError,
)
from .integrations import Integration
from .logging import configure_logging, setup_logger
from .loop import Loop, LoopManager
from .models import LoopEvent
from .monitor import LoopMonitor
from .scheduler import Schedule, validate_cron
from .state.state import StateManager, create_state_manager
from .task import TaskManager, TaskResult
from .types import BaseConfig, ExecutorType, LoopStatus, RetryPolicy
from .utils import get_func_import_path, import_func_from_path, infer_application_path
from .workflow import Workflow, WorkflowManager

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
    """Main application class that extends FastAPI with loop and workflow support."""

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
            await self.task_manager.stop_all()

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
        self.task_manager: TaskManager = TaskManager(self.state_manager)
        self._monitor_task: asyncio.Task[None] | None = None
        self._loop_start_func: Callable[[LoopContext], None] | None = None
        self._loop_metadata: dict[str, dict[str, Any]] = {}
        self._workflow_metadata: dict[str, dict[str, Any]] = {}
        self._task_metadata: dict[str, dict[str, Any]] = {}

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
        stop_after_idle_seconds: float | None = None,
        pause_after_idle_seconds: float | None = None,
    ) -> Callable[[Callable[..., Any] | type[Loop]], Callable[..., Any] | type[Loop]]:
        """Decorator to register a loop function or class."""
        if stop_after_idle_seconds is not None and pause_after_idle_seconds is not None:
            raise ValueError(
                "Cannot set both stop_after_idle_seconds and pause_after_idle_seconds"
            )

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
                    "stop_after_idle_seconds": stop_after_idle_seconds,
                    "pause_after_idle_seconds": pause_after_idle_seconds,
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

                await context.setup_integrations(event)
                await self.state_manager.push_event(loop.loop_id, event)

                if loop_instance:
                    loop_instance.ctx = context
                    await loop_instance.on_event(context, event)

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
                    stop_after_idle_seconds=stop_after_idle_seconds,
                    pause_after_idle_seconds=pause_after_idle_seconds,
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
                path=f"/{name}/{{loop_id}}/stop",
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
        """Decorator to register an event type."""

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
        """Decorator to register a workflow function or class."""

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
        """Restart a loop using stored metadata (keyed by loop name)."""
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

            await context.setup_integrations()

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
                stop_after_idle_seconds=metadata.get("stop_after_idle_seconds"),
                pause_after_idle_seconds=metadata.get("pause_after_idle_seconds"),
            )
            if started:
                logger.info("Restarted loop", extra={"loop_id": loop.loop_id})
                return True
            else:
                logger.warning(
                    "Failed to restart loop - task already exists in loop_manager",
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
        """Check if a loop has any active SSE client connections."""
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
                logger.info(
                    "Restarted workflow",
                    extra={"workflow_run_id": workflow.workflow_run_id},
                )
            return started

        except Exception as e:
            logger.error(
                "Failed to restart workflow",
                extra={"workflow_run_id": workflow_run_id, "error": str(e)},
            )
            return False

    def task(
        self,
        name: str,
        retry: RetryPolicy | None = None,
        executor: ExecutorType = ExecutorType.ASYNC,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a task. Creates POST /{name} and GET /{name}/{task_id} endpoints."""

        def _decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            if name in self._task_metadata:
                raise LoopAlreadyDefinedError(f"Task {name} already registered")

            self._task_metadata[name] = {
                "func": func,
                "retry": retry,
                "executor": executor,
            }

            async def _invoke_handler(request: dict[str, Any]):
                result = await self.task_manager.submit(
                    func=func,
                    args=request,
                    task_name=name,
                    retry_policy=retry,
                    executor_type=executor,
                )
                return JSONResponse(
                    content={"task_id": result.task_id, "status": "pending"},
                    status_code=HTTPStatus.ACCEPTED,
                )

            async def _status_handler(task_id: str):
                try:
                    task = await self.state_manager.get_task(task_id)
                    return JSONResponse(content=task.to_dict())
                except TaskNotFoundError as e:
                    raise HTTPException(
                        status_code=HTTPStatus.NOT_FOUND, detail=str(e)
                    ) from e

            self.add_api_route(f"/{name}", _invoke_handler, methods=["POST"])
            self.add_api_route(f"/{name}/{{task_id}}", _status_handler, methods=["GET"])

            return func

        return _decorator

    async def invoke(
        self,
        task_name: str,
        wait: bool = False,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> str | TaskResult | Any:
        """Invoke a task. Returns task_id if wait=False, else waits and returns result."""
        if task_name not in self._task_metadata:
            raise ValueError(f"Unknown task: {task_name}")

        metadata = self._task_metadata[task_name]
        handle = await self.task_manager.submit(
            func=metadata["func"],
            args=kwargs,
            task_name=task_name,
            retry_policy=metadata.get("retry"),
            executor_type=metadata.get("executor", ExecutorType.ASYNC),
        )

        if wait:
            return await handle.result(timeout=timeout)

        return handle.task_id

    def schedule(
        self,
        name: str,
        cron: str | None = None,
        interval: float | None = None,
        retry: RetryPolicy | None = None,
        executor: ExecutorType = ExecutorType.ASYNC,
        args: dict[str, Any] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a scheduled task. Use cron="*/5 * * * *" or interval=60."""
        if not cron and not interval:
            raise ValueError("Must specify either cron or interval")

        if cron and not validate_cron(cron):
            raise ValueError(f"Invalid cron expression: {cron}")

        def _decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.task(name=name, retry=retry, executor=executor)(func)

            schedule = Schedule(
                task_name=name,
                cron=cron,
                interval_seconds=interval,
                args=args or {},
            )

            self._task_metadata[name]["schedule"] = schedule

            return func

        return _decorator

    async def schedule_task(
        self,
        task_name: str,
        cron: str | None = None,
        interval: float | None = None,
        args: dict[str, Any] | None = None,
        schedule_id: str | None = None,
    ) -> str:
        """Programmatically schedule a registered task. Returns the schedule_id."""
        if task_name not in self._task_metadata:
            raise ValueError(f"Unknown task: {task_name}")

        if not cron and not interval:
            raise ValueError("Must specify either cron or interval")

        if cron and not validate_cron(cron):
            raise ValueError(f"Invalid cron expression: {cron}")

        sid = schedule_id or task_name
        schedule = Schedule(
            task_name=task_name,
            cron=cron,
            interval_seconds=interval,
            args=args or {},
        )
        await self.state_manager.save_schedule(sid, schedule)
        return sid

    async def unschedule_task(self, schedule_id: str) -> None:
        """Remove a scheduled task by its schedule_id."""
        await self.state_manager.delete_schedule(schedule_id)
