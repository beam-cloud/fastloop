"""
Data models for fastloop.

This module contains all data models used across the fastloop package:
- LoopEvent: Base class for events
- LoopState: Persisted state for a running loop
- WorkflowBlock: A single step in a workflow
- WorkflowState: Persisted state for a running workflow
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .types import LoopEventSender, LoopStatus


class LoopEvent(BaseModel):
    """Base class for events sent between client and server."""

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
        """Return a JSON string representation of the event."""
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoopEvent":
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, data: str) -> "LoopEvent":
        dict_data = json.loads(data)
        return cls.from_dict(dict_data)


@dataclass
class LoopState:
    """Persisted state for a running loop."""

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


class WorkflowBlock(BaseModel):
    """A single step in a workflow. Extend with additional fields as needed."""

    text: str
    type: str


@dataclass
class WorkflowState:
    """Persisted state for a running workflow."""

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
