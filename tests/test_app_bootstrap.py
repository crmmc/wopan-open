from __future__ import annotations

import logging

import pytest

from openwopan.app import main as main_module
from openwopan.app.main import _application_args, _build_login_window, main
from openwopan.auth.web_login import ValidatedLoginUser, WebLoginCoordinator
from openwopan.storage.settings import AppSettings


def test_build_dependencies_wires_default_graph() -> None:
    from openwopan.app.bootstrap import build_dependencies

    dependencies = build_dependencies()

    assert dependencies.settings == AppSettings()
    assert isinstance(dependencies.web_login_coordinator, WebLoginCoordinator)
    assert dependencies.credential_store is dependencies.web_login_coordinator._credential_store
    assert callable(dependencies.file_browser_factory)


def test_wopan_session_validator_maps_protocol_user() -> None:
    from openwopan.app.bootstrap import _WopanSessionValidator
    from openwopan.wopan.client import ValidatedWopanUser

    captured: dict[str, str] = {}

    class _StubClient:
        def __init__(self, cookie_header: str) -> None:
            captured["cookie"] = cookie_header

        def validate_session(self, token: str) -> ValidatedWopanUser:
            captured["token"] = token
            return ValidatedWopanUser(account_id="user-1", display_name="User One")

    import openwopan.app.bootstrap as bootstrap_module

    original_client = bootstrap_module.WopanClient
    bootstrap_module.WopanClient = _StubClient  # type: ignore[assignment]
    try:
        validator = _WopanSessionValidator("cookie-header")
        user = validator.validate_session("token-value")
    finally:
        bootstrap_module.WopanClient = original_client  # type: ignore[assignment]

    assert captured == {"cookie": "cookie-header", "token": "token-value"}
    assert user.account_id == "user-1"
    assert user.display_name == "User One"


def test_build_login_window_returns_login_window(qapp: object) -> None:
    from openwopan.ui.login_window import LoginWindow

    window = _build_login_window()

    assert isinstance(window, LoginWindow)


def test_application_args_uses_sys_argv_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module.sys, "argv", ["openwopan", "--real"])
    assert _application_args(None) == ["openwopan", "--real"]


def test_main_runs_until_quit_with_smoke_test(
    qapp: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    """main() 冒烟路径：真实构建窗口与依赖，SMOKE_TEST 环境变量让事件循环立即退出。"""
    import os

    from PySide6.QtWidgets import QApplication

    class _SingletonQApplication:
        """Reuses the existing QApplication so main() runs against a live Qt app."""

        setHighDpiScaleFactorRoundingPolicy = staticmethod(
            QApplication.setHighDpiScaleFactorRoundingPolicy
        )

        def __init__(self, _args: list[str]) -> None:
            self._real = QApplication.instance()
            assert self._real is not None

        def setAttribute(self, *_args: object) -> None:
            pass

        def installTranslator(self, *_args: object) -> None:
            pass

        def setFont(self, *_args: object) -> None:
            pass

        def quit(self) -> None:
            pass

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(main_module, "QApplication", _SingletonQApplication)
    monkeypatch.setenv(main_module.SMOKE_TEST_ENV, "1")
    monkeypatch.setattr(
        main_module, "load_app_settings", lambda: AppSettings(log_level="INFO")
    )
    monkeypatch.setattr(
        main_module, "ensure_app_settings_file", lambda _s: tmp_path / "settings.json"  # type: ignore[operator]
    )
    monkeypatch.setattr(main_module, "configure_logging", lambda _s: tmp_path / "openwopan.log")  # type: ignore[operator]

    exit_code = main(["openwopan"])

    assert exit_code == 0
    assert os.environ[main_module.SMOKE_TEST_ENV] == "1"


def test_main_schedules_controller_start_without_smoke_test(
    qapp: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    """无 SMOKE_TEST 时 singleShot 回调应指向 controller.start；exec 被桩掉避免真实事件循环。"""
    from PySide6.QtWidgets import QApplication

    monkeypatch.delenv(main_module.SMOKE_TEST_ENV, raising=False)
    monkeypatch.setattr(
        main_module, "load_app_settings", lambda: AppSettings(log_level="INFO")
    )
    monkeypatch.setattr(
        main_module, "ensure_app_settings_file", lambda _s: tmp_path / "settings.json"  # type: ignore[operator]
    )
    monkeypatch.setattr(main_module, "configure_logging", lambda _s: tmp_path / "openwopan.log")  # type: ignore[operator]

    from PySide6.QtWidgets import QApplication

    class _SingletonQApplication:
        """Reuses the existing QApplication so main() runs against a live Qt app."""

        setHighDpiScaleFactorRoundingPolicy = staticmethod(
            QApplication.setHighDpiScaleFactorRoundingPolicy
        )

        def __init__(self, _args: list[str]) -> None:
            self._real = QApplication.instance()
            assert self._real is not None

        def setAttribute(self, *_args: object) -> None:
            pass

        def installTranslator(self, *_args: object) -> None:
            pass

        def setFont(self, *_args: object) -> None:
            pass

        def quit(self) -> None:
            pass

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(main_module, "QApplication", _SingletonQApplication)
    captured_callbacks: list[object] = []
    original_single_shot = main_module.QTimer.singleShot

    def fake_single_shot(_msec: int, callback: object) -> None:
        captured_callbacks.append(callback)

    monkeypatch.setattr(main_module.QTimer, "singleShot", staticmethod(fake_single_shot))

    exit_code = main(["openwopan"])

    assert exit_code == 0
    assert len(captured_callbacks) == 1
    assert callable(captured_callbacks[0])
    logging.getLogger("openwopan").info("main executed")
