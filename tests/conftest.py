from __future__ import annotations

import os
import sys
from collections.abc import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

import openwopan.ui.main_window as main_window_module


class SyncQThread(QThread):
    """QThread stand-in that runs workers synchronously on the main thread."""

    def start(self, *args: object, **kwargs: object) -> None:
        self.started.emit()

    def quit(self) -> None:
        self.finished.emit()


@pytest.fixture
def sync_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_window_module, "QThread", SyncQThread)
    monkeypatch.setattr(
        main_window_module.BrowserOperationWorker,
        "moveToThread",
        lambda self, thread: None,
    )
    monkeypatch.setattr(
        main_window_module.DownloadWorker,
        "moveToThread",
        lambda self, thread: None,
    )
    monkeypatch.setattr(
        main_window_module.UploadWorker,
        "moveToThread",
        lambda self, thread: None,
    )


@pytest.fixture
def qapp() -> Iterator[QApplication]:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    assert isinstance(app, QApplication)
    yield app
