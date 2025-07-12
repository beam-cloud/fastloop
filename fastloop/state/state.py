import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..types import LoopEventSender, LoopStatus, StateConfig, StateType

if TYPE_CHECKING:
    from ..loop import LoopEvent


@dataclass
class LoopState:
    loop_id: str
    loop_name: str | None = None
    created_at: int = field(default_factory=lambda: int(datetime.now().timestamp()))
    status: LoopStatus = LoopStatus.PENDING
    idle_timeout: float = 60.0
    last_event_at: int = field(default_factory=lambda: int(datetime.now().timestamp()))

    def to_json(self) -> str:
        return self.__dict__.copy()

    def to_string(self) -> str:
        return json.dumps(self.__dict__, default=str)

    @classmethod
    def from_json(cls, json_str: str) -> "LoopState":
        data = json.loads(json_str)
        return cls(**data)


class StateManager(ABC):
    @abstractmethod
    async def get_all_loop_ids(
        self,
    ) -> set[str]:
        pass

    @abstractmethod
    async def get_all_loops(
        self,
        status: LoopStatus | None = None,
    ) -> list[LoopState]:
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
        loop_name: str | None = None,
        loop_id: str | None = None,
        idle_timeout: float = 60.0,
    ) -> tuple[LoopState, bool]:
        pass

    @abstractmethod
    async def update_loop(self, loop_id: str, state: LoopState):
        pass

    @abstractmethod
    async def update_loop_status(self, loop_id: str, status: LoopStatus) -> LoopState:
        pass

    @abstractmethod
    async def get_event_history(self, loop_id: str) -> list["LoopEvent"]:
        pass

    @abstractmethod
    async def push_event(self, loop_id: str, event: "LoopEvent"):
        pass

    @abstractmethod
    async def pop_server_event(self, loop_id: str) -> "LoopEvent":
        pass

    @abstractmethod
    async def pop_event(
        self,
        loop_id: str,
        event: "LoopEvent",
        sender: LoopEventSender,
    ) -> "LoopEvent":
        pass

    @abstractmethod
    async def with_claim(self, loop_id: str):
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
    async def get_next_nonce(self, loop_id: str) -> int:
        """
        Get the next nonce for a loop.
        """
        pass

    @abstractmethod
    async def get_events_since(
        self, loop_id: str, since_timestamp: float
    ) -> dict[str, Any]:
        """
        Get events that occurred since the given timestamp.
        """
        pass


def create_state_manager(app_name: str, config: StateConfig) -> StateManager:
    from .state_redis import RedisStateManager

    if config.type == StateType.REDIS.value:
        return RedisStateManager(app_name=app_name, config=config.redis)
    else:
        raise ValueError(f"Invalid state manager type: {config.type}")
