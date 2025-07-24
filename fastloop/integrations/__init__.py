from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from ..types import IntegrationType

if TYPE_CHECKING:
    from ..fastloop import FastLoop


class Integration(ABC):
    @abstractmethod
    def type(self) -> IntegrationType:
        pass

    @abstractmethod
    def register(self, fastloop: "FastLoop", loop_name: str) -> None:
        pass

    @abstractmethod
    async def emit(self, event: Any) -> None:
        pass

    @abstractmethod
    def events(self) -> list[Any]:
        pass
