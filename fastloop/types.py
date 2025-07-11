from enum import Enum, StrEnum

from pydantic import BaseModel


class LoopStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    IDLE = "idle"
    STOPPED = "stopped"


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


class StateType(str, Enum):
    REDIS = "redis"
    S3 = "s3"


class StateConfig(BaseModel):
    type: StateType = StateType.REDIS.value
    redis: RedisConfig = RedisConfig()
    s3: S3Config = S3Config()


class BaseConfig(BaseModel):
    debug_mode: bool = False
    log_level: str = "INFO"
    pretty_print_logs: bool = True
    config_path: str = "/etc/fastloop.d/"
    loop_delay_s: float = 0.1
    sse_poll_interval_s: float = 0.1
    sse_keep_alive_s: float = 10.0
    port: int = 8000
    host: str = "localhost"
    state: StateConfig = StateConfig()

    class Config:
        extra = "allow"
