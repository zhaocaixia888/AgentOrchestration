"""Configuration management module."""

import os
import json
from typing import Any, Dict, Optional, Set

from src.common.errors import ConfigurationError


MAX_CONFIG_SIZE = 10 * 1024 * 1024  # 10MB


class Config:
    def __init__(self, config_path: Optional[str] = None):
        self._data: Dict[str, Any] = {}
        if config_path:
            self.load(config_path)
        self._load_env_overrides()

    def load(self, path: str) -> None:
        size = os.path.getsize(path)
        if size > MAX_CONFIG_SIZE:
            raise ConfigurationError(
                f"Config file too large: {size} bytes (max {MAX_CONFIG_SIZE} bytes)"
            )
        with open(path) as f:
            self._data = json.load(f)

    def _collect_known_keys(self, data: Dict, prefix: str = "") -> Set[str]:
        keys = set()
        for k, v in data.items():
            path = f"{prefix}.{k}" if prefix else k
            keys.add(path)
            if isinstance(v, dict):
                keys |= self._collect_known_keys(v, path)
        return keys

    def _load_env_overrides(self) -> None:
        known = self._collect_known_keys(self._data)
        prefix = "AO_"
        for key, value in os.environ.items():
            if key.startswith(prefix):
                config_key = key[len(prefix):].lower().replace("_", ".")
                if config_key in known:
                    self._set_nested(config_key, value)

    def _set_nested(self, key: str, value: Any) -> None:
        parts = key.split(".")
        current = self._data
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value

    def get(self, key: str, default: Any = None) -> Any:
        parts = key.split(".")
        current = self._data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
                if current is None:
                    return default
            else:
                return default
        return current

    def set(self, key: str, value: Any) -> None:
        self._set_nested(key, value)

    def to_dict(self) -> Dict:
        return self._data
