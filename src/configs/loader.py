"""Loads ``application.yaml`` and resolves ``"${ENV_VAR:default} | type"`` entries.

``application.yaml`` is the source of truth for configuration shape and defaults; environment
variables (including anything in a local ``.env``) override those defaults. Modules must read
configuration through :mod:`src.configs` rather than touching the environment directly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILE = Path(__file__).parent / "application.yaml"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# "${ENV_VAR:default} | type" — the default segment may be empty or contain ':' itself.
_ENTRY = re.compile(
    r"^\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)(?::(?P<default>.*?))?\}\s*\|\s*(?P<type>\w+)$"
)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(RuntimeError):
    """Raised when application.yaml is malformed or a value cannot be cast."""


def _load_env_file(path: Path) -> None:
    """Load ``KEY=value`` pairs from a .env file without overriding the real environment."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))  # noqa: TID251


def _cast(value: str, type_name: str, key: str) -> Any:
    if type_name == "str":
        return value
    if type_name == "int":
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"{key}: expected int, got {value!r}") from exc
    if type_name == "float":
        try:
            return float(value)
        except ValueError as exc:
            raise ConfigError(f"{key}: expected float, got {value!r}") from exc
    if type_name == "bool":
        lowered = value.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ConfigError(f"{key}: expected bool, got {value!r}")
    if type_name == "list":
        return [item.strip() for item in value.split(",") if item.strip()]
    raise ConfigError(f"{key}: unsupported type {type_name!r}")


def _resolve(value: str, key: str) -> Any:
    match = _ENTRY.match(value.strip())
    if match is None:
        raise ConfigError(
            f'{key}: expected the format "${{ENV_VAR:default}} | type", got {value!r}'
        )
    env_value = os.environ.get(match["var"])  # noqa: TID251
    resolved = env_value if env_value is not None else (match["default"] or "")
    return _cast(resolved, match["type"], key)


def load_config() -> dict[str, Any]:
    """Return the resolved configuration as ``{"SECTION_KEY": value}``."""
    _load_env_file(ENV_FILE)

    raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("application.yaml must be a mapping of sections")

    resolved: dict[str, Any] = {}
    for section, entries in raw.items():
        if not isinstance(entries, dict):
            raise ConfigError(f"section {section!r} must be a mapping")
        for name, value in entries.items():
            key = f"{section}_{name}".upper()
            if not isinstance(value, str):
                raise ConfigError(f"{key}: values must be strings, got {type(value).__name__}")
            resolved[key] = _resolve(value, key)
    return resolved
