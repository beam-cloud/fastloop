from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ..loop import LoopEvent
from ..types import IntegrationType

if TYPE_CHECKING:
    from ..fastloop import FastLoop


class Integration(ABC):
    @abstractmethod
    def handle_event(self, event: LoopEvent):
        pass

    @abstractmethod
    def type(self) -> IntegrationType:
        pass

    @abstractmethod
    def register(self, fastloop: "FastLoop") -> None:
        pass
