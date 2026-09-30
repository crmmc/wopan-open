"""Shared test configuration.

Hosts two session-wide mechanisms:

1. ``OPENWOPAN_MEMTRACE=1`` gated per-test RSS sampling hook (measurement only).
2. ``qt_memory_hygiene`` autouse fixture that releases the Qt widget trees a
   finished test leaves behind (see the 09-30-test-suite-memory-budget task).
"""

from __future__ import annotations

import gc
import os
import resource
import sys
from collections.abc import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QThread
from PySide6.QtWidgets import QApplication, QWidget

import openwopan.app.controller as controller_module
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
    monkeypatch.setattr(controller_module, "QThread", SyncQThread)
    monkeypatch.setattr(
        main_window_module.BrowserOperationWorker,
        "moveToThread",
        lambda self, thread: None,
    )
    monkeypatch.setattr(
        controller_module.ControllerOperationWorker,
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


# ---------------------------------------------------------------------------
# Memory trace (OPENWOPAN_MEMTRACE=1). Disabled by default: the only per-report
# cost is one ``is None`` check.
#
# The TSV sink writes through a fd opened once at session start via
# ``os.open``/``os.write``. Tests monkeypatch ``open``/``Path.open`` (e.g.
# tests/test_wopan_client_helpers.py::failing_open); anything routed through
# the patched Python I/O layer would be hijacked mid-test, so the hook only
# ever touches the raw fd.
#
# ``ru_maxrss`` is the process high-water RSS: bytes on macOS, KiB on Linux.
# ---------------------------------------------------------------------------

_MEMTRACE_FD: int | None = None
_MEMTRACE_WRITE = os.write
_MEMTRACE_CLOSE = os.close


def pytest_configure(config: pytest.Config) -> None:
    global _MEMTRACE_FD
    if os.environ.get("OPENWOPAN_MEMTRACE", "") != "1" or _MEMTRACE_FD is not None:
        return
    out_path = os.environ.get("OPENWOPAN_MEMTRACE_OUT", "/tmp/memtrace.tsv")
    try:
        _MEMTRACE_FD = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(_MEMTRACE_FD, b"nodeid\twhen\tru_maxrss\n")
    except OSError:
        _MEMTRACE_FD = None


def pytest_unconfigure(config: pytest.Config) -> None:
    global _MEMTRACE_FD
    if _MEMTRACE_FD is None:
        return
    try:
        _MEMTRACE_CLOSE(_MEMTRACE_FD)
    except OSError:
        pass
    _MEMTRACE_FD = None


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if _MEMTRACE_FD is None:
        return
    ru_maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    line = f"{report.nodeid}\t{report.when}\t{ru_maxrss}\n"
    try:
        _MEMTRACE_WRITE(_MEMTRACE_FD, line.encode("utf-8", "replace"))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Inter-test Qt memory hygiene. See design.md of the memory-budget task:
# every widget-constructing test used to leave its whole top-level tree in the
# shared QApplication, saturating the suite at ~1.09GB. The fixture snapshots
# the live top-level widgets at setup and, at teardown, schedules deletion of
# the widgets the test itself created, drains the deferred-delete queue,
# pumps pending events, and breaks Python-side reference cycles.
#
# Safety rules (qt-threading.md):
# - never joins/quits threads; worker teardown stays with the tests;
# - every Qt call is guarded so a shiboken-dead object or a slot raising
#   during event drainage can never fail a passing test;
# - monkeypatch (same scope, non-autouse) is undone before this fixture's
#   teardown runs, and none of the cleanup touches monkeypatchable Python
#   I/O anyway.
# ---------------------------------------------------------------------------


def _drain_deferred_deletions(app: QApplication) -> None:
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


_TEST_THREAD_KEEP_ALIVE: set[tuple[QThread, QWidget]] = set()


def _abandon_running_child_threads(widget: QWidget) -> None:
    """Detach still-running QThread children before the window is destroyed.

    Tests may finish while an unwaited worker thread (``QThread(self)``) is
    still running; destroying the parent window would then abort the process
    ("QThread: Destroyed while thread is still running"). This mirrors the
    accepted production close path (``MainWindow._abandon_thread_after_timeout``):
    ``setParent(None)`` plus a module-level keep-alive reference discarded on
    ``finished``. No join/quit/terminate — thread contracts stay with the
    tests and the product code (qt-threading.md).
    """
    try:
        threads = widget.findChildren(QThread)
    except RuntimeError:
        return
    for thread in threads:
        try:
            if not thread.isRunning():
                continue
            thread.setParent(None)
        except RuntimeError:
            continue
        keep = (thread, widget)  # widget wrapper keeps worker refs alive too
        _TEST_THREAD_KEEP_ALIVE.add(keep)
        thread.finished.connect(
            lambda keep=keep: _TEST_THREAD_KEEP_ALIVE.discard(keep)
        )


@pytest.fixture(autouse=True)
def qt_memory_hygiene() -> Iterator[None]:
    app = QApplication.instance()
    snapshot: set[int] = set()
    if app is not None:
        try:
            snapshot = {id(w) for w in QApplication.allWidgets()}
        except RuntimeError:  # pragma: no cover - shiboken-dead wrapper mid-iteration
            snapshot = set()
    yield
    if app is None:
        return
    _release_new_top_level_widgets(snapshot)


def _release_new_top_level_widgets(snapshot: set[int]) -> None:
    app = QApplication.instance()
    if app is None:
        return
    pending: list[QWidget] = []
    try:
        for widget in QApplication.allWidgets():
            if id(widget) in snapshot:
                continue
            try:
                if widget.parent() is not None:
                    continue  # dies with its parent
            except RuntimeError:
                continue  # wrapper outlived its C++ object
            pending.append(widget)
    except RuntimeError:  # pragma: no cover - a widget vanished mid-iteration
        pass
    if not pending:
        return
    for widget in pending:
        _abandon_running_child_threads(widget)
        try:
            widget.deleteLater()
        except RuntimeError:
            pass
    try:
        _drain_deferred_deletions(app)
    except Exception:  # noqa: BLE001 - hygiene must never fail a test
        pass
    try:
        gc.collect()
    except Exception:  # pragma: no cover - hygiene must never fail a test
        pass
