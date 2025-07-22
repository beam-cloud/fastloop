import json
import os
import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic.alias_generators import to_snake

from .constants import DEFAULT_SYSTEM_CONFIG_DIR
from .exceptions import InvalidConfigError
from .logging import setup_logger

try:
    from pydantic import BaseModel, ValidationError

    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False  # type: ignore
    BaseModel = object
    ValidationError = Exception

logger = setup_logger(__name__)

T = TypeVar("T")


class ConfigFormat(Enum):
    """Supported configuration file formats."""

    JSON = ".json"
    YAML = ".yaml"
    YML = ".yml"


def convert_dict_keys_to_snake_case(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively convert dict keys from camelCase to snake_case."""
    converted: dict[str, Any] = {}

    for key, value in data.items():
        snake_key = to_snake(key)
        if isinstance(value, dict):
            converted[snake_key] = convert_dict_keys_to_snake_case(value)  # type: ignore
        elif isinstance(value, list):
            converted[snake_key] = [
                convert_dict_keys_to_snake_case(item)  # type: ignore
                if isinstance(item, dict)
                else item
                for item in value  # type: ignore
            ]
        else:
            converted[snake_key] = value

    return converted


@dataclass
class ConfigManager:
    config_data: dict[str, Any] = field(default_factory=dict)
    tag: str = "key"
    debug_mode: bool = False
    config_type: type[Any] | None = None

    def __post_init__(self):
        """Initialize the configuration manager with defaults."""

        self.load_default_config()

        self.config_path = os.getenv("FASTLOOP_CONFIG_PATH", "")
        if self.config_path:
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

    def load_from_config_path(self) -> None:
        """Load configuration from the file specified in FASTLOOP_CONFIG_PATH."""

        if Path(self.config_path).exists():
            try:
                self.load_config_file(self.config_path)
            except Exception as e:
                logger.error(f"Failed to load config from {self.config_path}: {e}")

    def load_from_system_directory(self) -> None:
        """Load configuration files from /etc/fastloop.d/ directory."""

        system_config_dir = Path(DEFAULT_SYSTEM_CONFIG_DIR)
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
                snake_case_config = convert_dict_keys_to_snake_case(self.config_data)
                return self.config_type(**snake_case_config)
            except ValidationError as e:
                logger.error(f"Configuration validation failed: {e}")
                raise
        else:
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
