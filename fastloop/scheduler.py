import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from croniter import croniter


@dataclass
class Schedule:
    """Task schedule. Use cron for cron expressions or interval_seconds for fixed intervals."""

    task_name: str
    cron: str | None = None  # e.g. "*/5 * * * *" (every 5 min)
    interval_seconds: float | None = None  # e.g. 60.0 (every minute)
    args: dict[str, Any] = field(default_factory=dict)
    next_run: float | None = None
    enabled: bool = True

    def compute_next_run(self, base_time: float | None = None) -> float:
        base = base_time or time.time()
        if self.cron:
            return croniter(self.cron, datetime.fromtimestamp(base)).get_next(float)
        if self.interval_seconds:
            return base + self.interval_seconds
        raise ValueError("Schedule must have either cron or interval_seconds")

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Schedule":
        return cls(**data)


def validate_cron(expr: str) -> bool:
    """Check if a cron expression is valid."""
    try:
        croniter(expr)
        return True
    except (ValueError, KeyError):
        return False
