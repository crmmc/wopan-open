from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from openwopan.app import main as main_module
from openwopan.app.main import (
    _application_args,
    clear_session_marker,
    detect_and_clear_stale_marker,
    session_marker_path,
    write_session_marker,
)
from openwopan.storage.settings import AppSettings


def test_application_args_preserves_explicit_args() -> None:
    assert _application_args(["openwopan", "--flag"]) == ["openwopan", "--flag"]


def test_application_args_supplies_program_name_for_empty_args() -> None:
    assert _application_args([]) == ["openwopan"]


def test_session_marker_path_is_next_to_log_file(tmp_path: Path) -> None:
    assert session_marker_path(tmp_path / "openwopan.log") == tmp_path / "openwopan.running"


def test_write_and_clear_session_marker(tmp_path: Path) -> None:
    marker_path = tmp_path / "openwopan.running"

    write_session_marker(marker_path)
    assert marker_path.exists()

    clear_session_marker(marker_path)
    assert not marker_path.exists()
    clear_session_marker(marker_path)


def test_detect_and_clear_stale_marker(tmp_path: Path) -> None:
    marker_path = tmp_path / "openwopan.running"

    assert detect_and_clear_stale_marker(marker_path) is False

    marker_path.touch()
    assert detect_and_clear_stale_marker(marker_path) is True
    assert not marker_path.exists()


@pytest.mark.parametrize(
    ("stale_marker", "smoke_test", "expected_notice_calls"),
    [(False, False, 0), (True, False, 1), (True, True, 0)],
)
def test_main_handles_session_marker_and_crash_dialogs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stale_marker: bool,
    smoke_test: bool,
    expected_notice_calls: int,
) -> None:
    log_path = tmp_path / "openwopan.log"
    marker_path = session_marker_path(log_path)
    if stale_marker:
        marker_path.touch()
    notice_paths: list[Path] = []
    crash_dialog_paths: list[Path] = []
    crash_callbacks: list[Callable[[Path], None]] = []

    class _FakeApplication:
        setHighDpiScaleFactorRoundingPolicy = staticmethod(lambda _policy: None)

        def __init__(self, _args: list[str]) -> None:
            pass

        def setAttribute(self, *_args: object) -> None:
            pass

        def installTranslator(self, _translator: object) -> None:
            pass

        def setFont(self, _font: object) -> None:
            pass

        def quit(self) -> None:
            pass

        def exec(self) -> int:
            return 0

    dependencies = SimpleNamespace(
        credential_store=object(),
        web_login_coordinator=object(),
        file_browser_factory=object(),
    )
    monkeypatch.setattr(main_module, "load_app_settings", lambda: AppSettings())
    monkeypatch.setattr(main_module, "ensure_app_settings_file", lambda _settings: tmp_path)
    monkeypatch.setattr(main_module, "configure_logging", lambda _settings: log_path)

    def _install_crash_reporting(
        path: Path,
        *,
        on_crash: Callable[[Path], None],
    ) -> Path:
        assert path == log_path
        crash_callbacks.append(on_crash)
        return tmp_path / "openwopan-crash.log"

    monkeypatch.setattr(main_module, "install_crash_reporting", _install_crash_reporting)
    monkeypatch.setattr(main_module, "QApplication", _FakeApplication)
    monkeypatch.setattr(main_module, "FluentTranslator", lambda: object())
    monkeypatch.setattr(main_module, "setTheme", lambda _theme: None)
    monkeypatch.setattr(main_module, "build_dependencies", lambda: dependencies)
    monkeypatch.setattr(main_module, "AppDependencies", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(main_module, "MainWindow", lambda **_kwargs: object())
    monkeypatch.setattr(
        main_module,
        "ApplicationController",
        lambda *_args: SimpleNamespace(start=lambda: None),
    )
    monkeypatch.setattr(main_module.QTimer, "singleShot", staticmethod(lambda *_args: None))
    monkeypatch.setattr(main_module, "show_crash_dialog", crash_dialog_paths.append)
    monkeypatch.setattr(
        main_module,
        "show_unclean_shutdown_notice",
        lambda path: notice_paths.append(path) or True,
    )
    if smoke_test:
        monkeypatch.setenv(main_module.SMOKE_TEST_ENV, "1")
    else:
        monkeypatch.delenv(main_module.SMOKE_TEST_ENV, raising=False)

    assert main_module.main(["openwopan"]) == 0

    assert len(crash_callbacks) == 1
    crash_callbacks[0](log_path)
    assert crash_dialog_paths == [tmp_path]
    assert notice_paths == [tmp_path] * expected_notice_calls
    assert not marker_path.exists()
