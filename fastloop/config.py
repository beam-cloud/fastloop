import json
import os
import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

import yaml

from .exceptions import InvalidConfigError
from .logging import setup_logger

try:
    from pydantic import BaseModel, ValidationError

    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False
    BaseModel = object
    ValidationError = Exception

logger = setup_logger(__name__)

T = TypeVar("T")


class ConfigFormat(Enum):
    """Supported configuration file formats."""

    JSON = ".json"
    YAML = ".yaml"
    YML = ".yml"


@dataclass
class ConfigManager:
    """
    A configuration manager that allows handling and manipulation
    of configuration data for various types.

    This is similar to the Go version's ConfigManager[T] struct, providing
    type-safe configuration management with support for YAML, JSON, and
    environment variables.
    """

    config_data: dict[str, Any] = field(default_factory=dict)
    tag: str = "key"
    debug_mode: bool = False
    config_type: type[Any] | None = None

    def __post_init__(self):
        """Initialize the configuration manager with defaults."""

        self.load_default_config()
        self.load_from_environment()
        self.load_from_config_path()
        self.load_from_system_directory()
        self.load_from_json_environment()

        if self.debug_mode:
            logger.info(
                f"Debug mode enabled. Current configuration: {self.print_config()}"
            )

    def load_default_config(self) -> None:
        """Load default configuration from config.default.yaml file."""

        default_config_path = Path(__file__).parent / "config.default.yaml"
        if default_config_path.exists():
            try:
                self.load_config_file(str(default_config_path))
                logger.info(f"Loaded default configuration from {default_config_path}")
            except BaseException as err:
                logger.error(
                    f"Failed to load default config from {default_config_path}: {traceback.format_exc()}"
                )
                raise InvalidConfigError(
                    f"Failed to load default config from {default_config_path}"
                ) from err

    def load_from_environment(self) -> None:
        """Load configuration from environment variables."""

        # Load common environment variables
        env_config = {
            "debugMode": os.getenv("DEBUG_MODE", "false").lower() == "true",
            "logLevel": os.getenv("LOG_LEVEL", "INFO"),
            "configPath": os.getenv("CONFIG_PATH", ""),
        }
        self.config_data.update(env_config)

    def load_from_config_path(self) -> None:
        """Load configuration from the file specified in CONFIG_PATH."""

        config_path = self.config_data.get("configPath")
        if config_path and Path(config_path).exists():
            try:
                self.load_config_file(config_path)
            except Exception as e:
                logger.error(f"Failed to load config from {config_path}: {e}")

    def load_from_system_directory(self) -> None:
        """Load configuration files from /etc/fastloop.d/ directory."""

        system_config_dir = Path("/etc/fastloop.d/")
        if system_config_dir.exists():
            for config_file in sorted(system_config_dir.glob("*")):
                if config_file.suffix in [".yaml", ".yml", ".json"]:
                    try:
                        self.load_config_file(str(config_file))
                    except Exception as e:
                        logger.error(f"Failed to load config from {config_file}: {e}")

    def load_from_json_environment(self) -> None:
        """Load configuration from CONFIG_JSON environment variable."""

        config_json = os.getenv("CONFIG_JSON")
        if config_json:
            try:
                json_config = json.loads(config_json)
                self.config_data.update(json_config)
                self.tag = "json"
            except Exception as e:
                logger.error(f"Failed to load config from CONFIG_JSON: {e}")

    def load_config_file(self, file_path: str) -> None:
        """Load configuration from a file based on its extension."""

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {file_path}")

        with open(path, encoding="utf-8") as f:
            content = f.read()

        if path.suffix in [".yaml", ".yml"]:
            yaml_config = yaml.safe_load(content)
            if yaml_config:
                self.config_data.update(yaml_config)
        elif path.suffix == ".json":
            json_config = json.loads(content)
            self.config_data.update(json_config)
        else:
            raise ValueError(f"Unsupported configuration format: {path.suffix}")

    def print_config(self) -> str:
        """Return a string representation of the current configuration."""
        return json.dumps(self.config_data, indent=2, default=str)

    def get_config(self) -> Any:
        """
        Retrieve the current configuration.

        If Pydantic is available and config_type is a Pydantic model,
        this will validate and convert the configuration data to the
        specified type. Otherwise, it returns the raw configuration data.
        """
        if (
            PYDANTIC_AVAILABLE
            and self.config_type
            and issubclass(self.config_type, BaseModel)
        ):
            try:
                # Convert the config data to the Pydantic model
                return self.config_type(**self.config_data)
            except ValidationError as e:
                logger.error(f"Configuration validation failed: {e}")
                raise
        else:
            # Return the config_data as-is for non-Pydantic types
            return self.config_data

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value by key."""
        return self.config_data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set a configuration value."""
        self.config_data[key] = value

    def has(self, key: str) -> bool:
        """Check if a configuration key exists."""
        return key in self.config_data

    def reload(self) -> None:
        """Reload configuration from all sources."""
        self.config_data.clear()
        self.__post_init__()


def create_config_manager(config_type: type[Any] | None = None) -> ConfigManager:
    """
    Create a new configuration manager instance.

    Args:
        config_type: The type of configuration object to create (optional)

    Returns:
        A new ConfigManager instance
    """
    return ConfigManager(config_type=config_type)
