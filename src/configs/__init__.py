"""Typed access to the resolved application configuration.

Import the names directly::

    from src.configs import DATABASE_URL, SERVER_PORT

Names are ``SECTION_KEY`` in upper snake case, derived from ``application.yaml``. Regenerate the
type stub with ``python -m src.configs.generate`` after adding or renaming a key.
"""

from __future__ import annotations

from typing import Any

from src.configs.loader import ConfigError, load_config

__all__ = ["ConfigError", "load_config", "reload"]

_config: dict[str, Any] = load_config()


def reload() -> None:
    """Re-read application.yaml and the environment. Intended for tests."""
    global _config
    _config = load_config()


def __getattr__(name: str) -> Any:
    try:
        return _config[name]
    except KeyError:
        raise AttributeError(
            f"{name!r} is not defined in application.yaml — add it there rather than reading "
            f"the environment directly."
        ) from None


def __dir__() -> list[str]:
    return sorted([*__all__, *_config])
