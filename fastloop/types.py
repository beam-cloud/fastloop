from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from .loop import LoopEvent

E = TypeVar("E", bound="LoopEvent")


class LoopStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    IDLE = "idle"
    STOPPED = "stopped"
    FAILED = "failed"


class ScheduleType(StrEnum):
    IMMEDIATE = "immediate"
    DELAY = "delay"
    STOP = "stop"


class ExecutorType(StrEnum):
    """How to run the task: ASYNC (default), THREAD (ThreadPool), PROCESS (ProcessPool)."""

    ASYNC = "async"
    THREAD = "thread"
    PROCESS = "process"


class TaskStatus(StrEnum):
    """Task lifecycle status."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"


@dataclass
class BlockPlan:
    next_block_index: int | None = None
    schedule_type: ScheduleType = ScheduleType.IMMEDIATE
    delay_seconds: float | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "next_block_index": self.next_block_index,
            "schedule_type": self.schedule_type.value,
            "delay_seconds": self.delay_seconds,
            "reason": self.reason,
        }


@dataclass
class RetryPolicy:
    """Retry configuration with exponential backoff."""

    max_attempts: int = 3
    initial_delay: float = 1.0  # seconds
    max_delay: float = 60.0  # seconds
    backoff_multiplier: float = 2.0

    def compute_delay(self, attempt: int) -> float:
        delay = self.initial_delay * (self.backoff_multiplier ** (attempt - 1))
        return min(delay, self.max_delay)


class LoopEventSender(StrEnum):
    CLIENT = "client"
    SERVER = "server"


class RedisConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 6379
    database: int = 0
    password: str = ""
    ssl: bool = False


class S3Config(BaseModel):
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    region_name: str = "us-east-1"
    bucket_name: str = "fastloop"
    prefix: str = "fastloop"
    endpoint_url: str = ""


class StateType(str, Enum):
    REDIS = "redis"
    S3 = "s3"


class StateConfig(BaseModel):
    type: str = StateType.REDIS.value
    redis: RedisConfig = RedisConfig()
    s3: S3Config = S3Config()


class CorsConfig(BaseModel):
    enabled: bool = True
    allow_origins: list[str] = ["*"]
    allow_credentials: bool = True
    allow_methods: list[str] = ["*"]
    allow_headers: list[str] = ["*"]


class IntegrationType(StrEnum):
    SLACK = "slack"
    SURGE = "surge"
    TELNYX = "telnyx"


class BaseConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    debug_mode: bool = False
    log_level: str = "INFO"
    pretty_print_logs: bool = True
    loop_delay_s: float = 0.1
    sse_poll_interval_s: float = 0.1
    sse_keep_alive_s: float = 10.0
    shutdown_timeout_s: float = 10.0
    port: int = 8000
    host: str = "localhost"
    cors: CorsConfig = CorsConfig()
    state: StateConfig = StateConfig()
