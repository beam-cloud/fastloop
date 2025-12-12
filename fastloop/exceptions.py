from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from .context import LoopContext

T = TypeVar("T", bound="LoopContext")


class InvalidConfigError(Exception):
    pass


class LoopNotFoundError(Exception):
    pass


class LoopClaimError(Exception):
    pass


class LoopPausedError(Exception):
    pass


class LoopStoppedError(Exception):
    pass


class LoopAlreadyDefinedError(Exception):
    pass


class EventTimeoutError(Exception):
    pass


class LoopContextSwitchError(Exception):
    def __init__(self, func: Callable[[T], Awaitable[None]], context: "LoopContext"):
        self.func = func
        self.context = context


class WorkflowNextError(Exception):
    def __init__(self, payload: dict | None = None):
        self.payload = payload


class WorkflowRepeatError(Exception):
    pass


class WorkflowGotoError(Exception):
    def __init__(
        self,
        block_index: int,
        delay_seconds: float | None = None,
        reason: str | None = None,
    ):
        self.block_index = block_index
        self.delay_seconds = delay_seconds
        self.reason = reason


class WorkflowNotFoundError(Exception):
    pass


class TaskNotFoundError(Exception):
    pass


class WorkflowMaxRetriesError(Exception):
    def __init__(
        self,
        workflow_run_id: str,
        block_index: int,
        attempts: int,
        last_error: str | None = None,
    ):
        self.workflow_run_id = workflow_run_id
        self.block_index = block_index
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"Workflow run {workflow_run_id} block {block_index} failed after {attempts} attempts"
        )
