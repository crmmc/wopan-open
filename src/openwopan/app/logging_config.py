from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from platformdirs import user_log_path

from openwopan.storage.settings import APP_AUTHOR, APP_NAME, AppSettings

LOG_FILE_NAME = "openwopan.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def app_log_path() -> Path:
    """Return the application log file path."""
    return user_log_path(APP_NAME, APP_AUTHOR) / LOG_FILE_NAME


def configure_logging(settings: AppSettings, log_path: Path | None = None) -> Path:
    """Configure OpenWoPan file logging without recording credential material."""
    target_path = log_path or app_log_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("openwopan")
    set_logging_level(settings.log_level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = RotatingFileHandler(
        target_path,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    return target_path


def set_logging_level(level_name: str) -> None:
    """Apply the OpenWoPan logger level at runtime."""
    level = getattr(logging, level_name.upper(), None)
    if not isinstance(level, int):
        raise ValueError("invalid logging level")
    logger = logging.getLogger("openwopan")
    logger.setLevel(level)
    for handler in logger.handlers:
        handler.setLevel(level)
