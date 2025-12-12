import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .types import LoopEventSender, LoopStatus, TaskStatus


class LoopEvent(BaseModel):
    loop_id: str | None = None
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    partial: bool = False
    type: str = Field(default_factory=lambda: getattr(LoopEvent, "type", ""))
    sender: LoopEventSender = LoopEventSender.CLIENT
    timestamp: float = Field(default_factory=lambda: datetime.now().timestamp())
    nonce: int | None = None

    def __init__(self, **data: Any) -> None:
        if "type" not in data and hasattr(self.__class__, "type"):
            data["type"] = self.__class__.type
        super().__init__(**data)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    def to_string(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoopEvent":
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, data: str) -> "LoopEvent":
        return cls.from_dict(json.loads(data))


@dataclass
class LoopState:
    loop_id: str
    loop_name: str | None = None
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    status: LoopStatus = LoopStatus.PENDING
    current_function_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    def to_string(self) -> str:
        return json.dumps(self.__dict__, default=str)

    @classmethod
    def from_json(cls, json_str: str) -> "LoopState":
        return cls(**json.loads(json_str))


class WorkflowBlock(BaseModel):
    text: str
    type: str


@dataclass
class WorkflowState:
    workflow_run_id: str
    workflow_name: str | None = None
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    status: LoopStatus = LoopStatus.PENDING
    blocks: list[dict[str, Any]] = field(default_factory=list)
    current_block_index: int = 0
    next_payload: dict[str, Any] | None = None
    completed_blocks: list[int] = field(default_factory=list)
    block_attempts: dict[int, int] = field(default_factory=dict)
    last_error: str | None = None
    last_block_output: Any = None
    scheduled_wake_time: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["block_attempts"] = {str(k): v for k, v in self.block_attempts.items()}
        return d

    def to_string(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_json(cls, json_str: str) -> "WorkflowState":
        data = json.loads(json_str)
        if "block_attempts" in data and isinstance(data["block_attempts"], dict):
            data["block_attempts"] = {
                int(k): v for k, v in data["block_attempts"].items()
            }
        return cls(**data)


@dataclass
class TaskState:
    task_id: str
    task_name: str
    status: TaskStatus = TaskStatus.PENDING
    args: dict[str, Any] = field(default_factory=dict)
    result: Any | None = None
    error: str | None = None
    attempts: int = 0
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    completed_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    def to_string(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_json(cls, json_str: str) -> "TaskState":
        return cls(**json.loads(json_str))
