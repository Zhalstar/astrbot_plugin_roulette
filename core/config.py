from __future__ import annotations

from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
)


class ConfigNode:
    """Provide typed attribute access to plugin configuration data."""

    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {key for key in cls._schema() if not key.startswith("_")},
        )

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        for key in self._fields():
            if key in data or hasattr(self.__class__, key):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] missing key: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            return self._data.get(key)
        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def save_config(self) -> None:
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() only supports AstrBotConfig"
            )
        self._data.save_config()


class PluginConfig(ConfigNode):
    """Manage all roulette plugin configuration."""

    max_ban_duration: int
    game_timeout: int
    persuasion_quotes: list[str]

    _plugin_name = "astrbot_plugin_roulette"

    def __init__(self, cfg: AstrBotConfig):
        super().__init__(cfg)
        self.data_dir = Path(get_astrbot_plugin_data_path()) / self._plugin_name
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.stats_file = self.data_dir / "roulette_stats.json"
