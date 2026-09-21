from __future__ import annotations

import faulthandler
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Any

from platformdirs import user_log_path

from openwopan.storage.settings import APP_AUTHOR, APP_NAME, AppSettings

LOG_FILE_NAME = "openwopan.log"
CRASH_LOG_FILE_NAME = "openwopan-crash.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

LOGGER = logging.getLogger(__name__)
_fault_handler_stream: IO[str] | None = None


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


def install_crash_reporting(log_path: Path) -> Path | None:
    """Leave evidence in the logs when the process dies unexpectedly.

    Unhandled Python exceptions are written to the rotating application log,
    and native faults (e.g. Qt crashes in threads) are dumped to a crash file
    next to it via faulthandler. Requires configure_logging() first so the
    exception records reach a file instead of a discarded stderr.
    """
    global _fault_handler_stream
    crash_path = log_path.parent / CRASH_LOG_FILE_NAME
    try:
        crash_path.parent.mkdir(parents=True, exist_ok=True)
        _fault_handler_stream = open(crash_path, "a", encoding="utf-8")
        faulthandler.enable(file=_fault_handler_stream)
    except OSError:
        LOGGER.warning("app.crash.faulthandler_unavailable")
        return None

    previous_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def _excepthook(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            LOGGER.error("app.crash.unhandled_exception", exc_info=(exc_type, exc_value, exc_tb))
        previous_hook(exc_type, exc_value, exc_tb)

    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        thread_name = getattr(args.thread, "name", None)
        exc_value = args.exc_value
        if exc_value is not None:
            LOGGER.error(
                "app.crash.thread_exception thread=%s",
                thread_name,
                exc_info=(args.exc_type, exc_value, args.exc_traceback),
            )
        else:
            LOGGER.error(
                "app.crash.thread_exception thread=%s exc_type=%s",
                thread_name,
                args.exc_type.__name__,
            )
        previous_thread_hook(args)

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    return crash_path
