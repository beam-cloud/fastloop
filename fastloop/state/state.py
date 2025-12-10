import json
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import datetime
from queue import Queue
from typing import TYPE_CHECKING, Any

from ..types import E, LoopEventSender, LoopStatus, StateConfig, StateType

if TYPE_CHECKING:
    from ..loop import LoopEvent, WorkflowState


@dataclass
class LoopState:
    loop_id: str
    loop_name: str | None = None
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    status: LoopStatus = LoopStatus.PENDING
    current_function_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a dictionary representation of the loop state."""
        return self.__dict__.copy()

    def to_string(self) -> str:
        """Return a JSON string representation of the loop state."""
        return json.dumps(self.__dict__, default=str)

    @classmethod
    def from_json(cls, json_str: str) -> "LoopState":
        data = json.loads(json_str)
        return cls(**data)


class StateManager(ABC):
    @abstractmethod
    async def get_all_loop_ids(self) -> set[str]:
        pass

    @abstractmethod
    async def get_all_loops(self, status: LoopStatus | None = None) -> list[LoopState]:
        pass

    @abstractmethod
    async def get_loop(
        self,
        loop_id: str,
    ) -> LoopState:
        pass

    @abstractmethod
    async def get_or_create_loop(
        self,
        *,
        loop_name: str | None = None,
        loop_id: str | None = None,
        current_function_path: str = "",
    ) -> tuple[LoopState, bool]:
        pass

    @abstractmethod
    async def update_loop(self, loop_id: str, state: LoopState):
        pass

    @abstractmethod
    async def update_loop_status(self, loop_id: str, status: LoopStatus) -> LoopState:
        pass

    @abstractmethod
    async def get_event_history(self, loop_id: str) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    async def push_event(self, loop_id: str, event: "LoopEvent"):
        pass

    @abstractmethod
    async def pop_server_event(self, loop_id: str) -> dict[str, Any] | None:
        pass

    @abstractmethod
    async def set_loop_mapping(self, external_ref_id: str, loop_id: str):
        pass

    @abstractmethod
    async def get_loop_mapping(self, external_ref_id: str) -> str | None:
        pass

    @abstractmethod
    async def pop_event(
        self,
        loop_id: str,
        event: type[E],
        sender: LoopEventSender,
    ) -> E | None:
        pass

    @abstractmethod
    async def set_wake_time(self, loop_id: str, timestamp: float) -> None:
        pass

    @abstractmethod
    async def with_claim(self, loop_id: str) -> AsyncGenerator[None, None]:
        pass

    @abstractmethod
    async def has_claim(self, loop_id: str) -> bool:
        pass

    @abstractmethod
    async def get_context_value(self, loop_id: str, key: str) -> Any:
        pass

    @abstractmethod
    async def set_context_value(self, loop_id: str, key: str, value: Any):
        pass

    @abstractmethod
    async def get_initial_event(self, loop_id: str) -> "LoopEvent | None":
        pass

    @abstractmethod
    async def delete_context_value(self, loop_id: str, key: str):
        pass

    @abstractmethod
    async def get_next_nonce(self, loop_id: str) -> int:
        """
        Get the next nonce for a loop.
        """
        pass

    @abstractmethod
    async def get_events_since(
        self, loop_id: str, since_timestamp: float
    ) -> list[dict[str, Any]]:
        """
        Get events that occurred since the given timestamp.
        """
        pass

    @abstractmethod
    async def subscribe_to_events(self, loop_id: str) -> Any:
        """Subscribe to event notifications for a specific loop"""
        pass

    @abstractmethod
    async def wait_for_event_notification(
        self, pubsub: Any, timeout: float | None = None
    ) -> bool:
        """Wait for an event notification or timeout"""
        pass

    @abstractmethod
    async def register_client_connection(
        self, loop_id: str, connection_id: str
    ) -> None:
        """Register an active SSE client connection for a loop"""
        pass

    @abstractmethod
    async def unregister_client_connection(
        self, loop_id: str, connection_id: str
    ) -> None:
        """Unregister an SSE client connection for a loop"""
        pass

    @abstractmethod
    async def get_active_client_count(self, loop_id: str) -> int:
        """Get the number of active SSE client connections for a loop"""
        pass

    @abstractmethod
    async def refresh_client_connection(self, loop_id: str, connection_id: str) -> None:
        """Refresh the TTL for an active SSE client connection"""
        pass

    @abstractmethod
    async def try_acquire_app_start_lock(self, loop_id: str) -> bool:
        """
        Try to acquire an app start lock for a loop.
        Returns True if lock acquired, False if already held.
        Lock should be released after on_app_start completes.
        """
        pass

    @abstractmethod
    async def release_app_start_lock(self, loop_id: str) -> None:
        """Release the app start lock for a loop."""
        pass

    @abstractmethod
    async def get_workflow(self, workflow_run_id: str) -> "WorkflowState":
        pass

    @abstractmethod
    async def get_or_create_workflow(
        self,
        *,
        workflow_name: str | None = None,
        workflow_run_id: str | None = None,
        blocks: list[dict[str, Any]],
    ) -> tuple["WorkflowState", bool]:
        pass

    @abstractmethod
    async def update_workflow(
        self, workflow_run_id: str, state: "WorkflowState"
    ) -> None:
        pass

    @abstractmethod
    async def update_workflow_status(
        self, workflow_run_id: str, status: LoopStatus
    ) -> "WorkflowState":
        pass

    @abstractmethod
    async def update_workflow_block_index(
        self, workflow_run_id: str, index: int, payload: dict[str, Any] | None = None
    ) -> None:
        pass

    @abstractmethod
    async def get_workflow_blocks(self, workflow_run_id: str) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    async def workflow_has_claim(self, workflow_run_id: str) -> bool:
        pass

    @abstractmethod
    async def with_workflow_claim(
        self, workflow_run_id: str
    ) -> AsyncGenerator[None, None]:
        pass

    @abstractmethod
    async def get_all_workflows(
        self, status: LoopStatus | None = None
    ) -> list["WorkflowState"]:
        pass

    @abstractmethod
    async def set_workflow_wake_time(
        self, workflow_run_id: str, timestamp: float
    ) -> None:
        pass

    @abstractmethod
    async def clear_workflow_wake_time(self, workflow_run_id: str) -> None:
        pass

    @abstractmethod
    async def try_claim_workflow_wake(self, workflow_run_id: str) -> bool:
        """Atomically try to claim a workflow wake. Returns True if this caller won the race."""
        pass

    @abstractmethod
    async def set_workflow_block_output(
        self, workflow_run_id: str, output: Any
    ) -> None:
        pass

    @abstractmethod
    async def get_workflow_block_output(self, workflow_run_id: str) -> Any:
        pass


def create_state_manager(
    *,
    app_name: str,
    config: StateConfig,
    wake_queue: Queue[str],
) -> StateManager:
    from .state_redis import RedisStateManager
    from .state_s3 import S3StateManager

    if config.type == StateType.REDIS.value:
        return RedisStateManager(
            app_name=app_name,
            config=config.redis,
            wake_queue=wake_queue,
        )
    elif config.type == StateType.S3.value:
        return S3StateManager(app_name=app_name, config=config.s3)
    else:
        raise ValueError(f"Invalid state manager type: {config.type}")
