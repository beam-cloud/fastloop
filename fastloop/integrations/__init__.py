from abc import ABC, abstractmethod

from ..loop import LoopEvent
from ..types import IntegrationType


class Integration(ABC):
    @abstractmethod
    def handle_event(self, event: LoopEvent):
        pass

    @abstractmethod
    def type(self) -> IntegrationType:
        pass
