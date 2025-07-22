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
