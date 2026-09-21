from __future__ import annotations

import faulthandler
import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from openwopan.app import logging_config
from openwopan.app.logging_config import (
    configure_logging,
    install_crash_reporting,
    set_logging_level,
)
from openwopan.storage.settings import AppSettings


def test_configure_logging_writes_openwopan_log_file(tmp_path: object) -> None:
    log_path = tmp_path / "openwopan.log"  # type: ignore[operator]

    configured_path = configure_logging(AppSettings(log_level="DEBUG"), log_path)
    logger = logging.getLogger("openwopan.test")
    logger.debug("debug-visible")

    for handler in logging.getLogger("openwopan").handlers:
        handler.flush()

    assert configured_path == log_path
    assert "debug-visible" in log_path.read_text(encoding="utf-8")


def test_set_logging_level_applies_to_openwopan_logger(tmp_path: object) -> None:
    log_path = tmp_path / "openwopan.log"  # type: ignore[operator]
    configure_logging(AppSettings(log_level="INFO"), log_path)

    set_logging_level("ERROR")

    assert logging.getLogger("openwopan").level == logging.ERROR
    for handler in logging.getLogger("openwopan").handlers:
        assert handler.level == logging.ERROR


def test_set_logging_level_rejects_unknown_level() -> None:
    with pytest.raises(ValueError, match="invalid logging level"):
        set_logging_level("NOPE")


@pytest.fixture
def crash_reporting(tmp_path: Path):
    log_path = tmp_path / "openwopan.log"
    configure_logging(AppSettings(log_level="INFO"), log_path)
    previous_hook = sys.excepthook
    previous_thread_hook = threading.excepthook
    crash_path = install_crash_reporting(log_path)

    yield log_path, crash_path

    faulthandler.disable()
    stream = logging_config._fault_handler_stream
    if stream is not None and not stream.closed:
        stream.close()
    sys.excepthook = previous_hook
    threading.excepthook = previous_thread_hook
    for handler in logging.getLogger("openwopan").handlers:
        handler.flush()


def _read_log(log_path: Path) -> str:
    return log_path.read_text(encoding="utf-8")


def test_crash_reporting_logs_unhandled_exception(crash_reporting) -> None:
    log_path, _ = crash_reporting
    try:
        raise ValueError("boom")
    except ValueError as exc:
        tb = exc.__traceback__

    assert sys.excepthook is not None
    sys.excepthook(ValueError, ValueError("boom"), tb)

    content = _read_log(log_path)
    assert "app.crash.unhandled_exception" in content
    assert "ValueError: boom" in content


def test_crash_reporting_skips_keyboard_interrupt(crash_reporting) -> None:
    log_path, _ = crash_reporting
    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt as exc:
        tb = exc.__traceback__

    sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), tb)

    assert "app.crash.unhandled_exception" not in _read_log(log_path)


def test_crash_reporting_logs_thread_exception(crash_reporting) -> None:
    log_path, _ = crash_reporting
    try:
        raise RuntimeError("worker died")
    except RuntimeError as exc:
        tb = exc.__traceback__
        exc_value = exc
    args = SimpleNamespace(
        exc_type=RuntimeError,
        exc_value=exc_value,
        exc_traceback=tb,
        thread=SimpleNamespace(name="upload-worker"),
    )

    assert threading.excepthook is not None
    threading.excepthook(args)

    content = _read_log(log_path)
    assert "app.crash.thread_exception" in content
    assert "thread=upload-worker" in content
    assert "RuntimeError: worker died" in content

    bare_args = SimpleNamespace(
        exc_type=RuntimeError,
        exc_value=None,
        exc_traceback=None,
        thread=SimpleNamespace(name="bare-worker"),
    )
    threading.excepthook(bare_args)

    assert "thread=bare-worker exc_type=RuntimeError" in _read_log(log_path)


def test_crash_reporting_enables_faulthandler_with_crash_file(crash_reporting) -> None:
    _, crash_path = crash_reporting

    assert crash_path is not None
    assert faulthandler.is_enabled()
    assert crash_path.exists()


def test_crash_reporting_survives_unwritable_crash_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "openwopan.log"
    configure_logging(AppSettings(log_level="INFO"), log_path)
    previous_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def _raise_os_error(*args: object, **kwargs: object) -> object:
        raise OSError("disk full")

    monkeypatch.setattr(logging_config, "open", _raise_os_error, raising=False)

    assert install_crash_reporting(log_path) is None
    assert sys.excepthook is previous_hook
    assert threading.excepthook is previous_thread_hook

    for handler in logging.getLogger("openwopan").handlers:
        handler.flush()
    assert "app.crash.faulthandler_unavailable" in _read_log(log_path)
