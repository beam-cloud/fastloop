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
    endpoint_url: str = ""


class StateType(str, Enum):
    REDIS = "redis"
    S3 = "s3"


class StateConfig(BaseModel):
    type: StateType = StateType.REDIS.value
    redis: RedisConfig = RedisConfig()
    s3: S3Config = S3Config()


class CorsConfig(BaseModel):
    enabled: bool = True
    allow_origins: list[str] = ["*"]
    allow_credentials: bool = True
    allow_methods: list[str] = ["*"]
    allow_headers: list[str] = ["*"]


class BaseConfig(BaseModel):
    debug_mode: bool = False
    log_level: str = "INFO"
    pretty_print_logs: bool = True
    loop_delay_s: float = 0.1
    sse_poll_interval_s: float = 0.1
    sse_keep_alive_s: float = 10.0
    max_idle_cycles: int = 10
    port: int = 8000
    host: str = "localhost"
    cors: CorsConfig = CorsConfig()
    state: StateConfig = StateConfig()

    class Config:
        extra = "allow"
