"""Fixtures for the knowledge base suite."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from src import configs


@pytest.fixture
def config_override(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    """Temporarily change configuration the way a deployment would — through the environment.

    Reaching into the loaded dictionary would test a different code path from the one production
    uses; setting the variable and reloading exercises the real resolution, including the cast.
    """

    def override(**values: Any) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key, str(value))
        configs.reload()

    yield override

    monkeypatch.undo()
    configs.reload()
