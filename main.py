"""Uvicorn entrypoint: ``python main.py``."""

from __future__ import annotations

import uvicorn

from src import configs
from src.core.factory import create_app

app = create_app()


def main() -> None:
    uvicorn.run(
        "main:app",
        host=configs.SERVER_HOST,
        port=configs.SERVER_PORT,
        reload=configs.APP_DEBUG,
        log_level=configs.LOGGING_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
