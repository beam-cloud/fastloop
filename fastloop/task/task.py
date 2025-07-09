from abc import ABC, abstractmethod

from .task_beam import BeamTaskRunner


class TaskManager:
    def __init__(self):
        self.runner: TaskRunner = BeamTaskRunner()

    def register_task(self, task_id: str, task_name: str, task_description: str):
        pass

    def get_task(self, task_id: str):
        pass


class TaskRunner(ABC):
    @abstractmethod
    def execute(self):
        raise NotImplementedError("Subclasses must implement this method")
