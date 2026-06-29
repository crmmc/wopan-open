from __future__ import annotations

import logging

from openwopan.app.logging_config import configure_logging, set_logging_level
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
