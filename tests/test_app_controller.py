from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openwopan.app.bootstrap import AppDependencies
from openwopan.app.controller import ApplicationController
from openwopan.auth.session import AuthSession
from openwopan.auth.web_login import RestoredWebLogin
from openwopan.storage.credentials import CredentialStore
from openwopan.storage.settings import AppSettings
from openwopan.ui.main_window import MainWindow
from openwopan.wopan.models import WopanCloudUsage, WopanItem, WopanItemKind


class FakeSignal:
    def __init__(self) -> None:
        self.slots: list[Any] = []

    def connect(self, slot: Any) -> object:
        self.slots.append(slot)
        return object()

    def emit(self, value: str) -> None:
        for slot in self.slots:
            slot(value)


class FakeFinishedSignal:
    def __init__(self) -> None:
        self.slots: list[Any] = []

    def connect(self, slot: Any) -> object:
        self.slots.append(slot)
        return object()

    def emit(self, value: int) -> None:
        for slot in self.slots:
            slot(value)


class FakeLoginWindow:
    def __init__(self) -> None:
        self.cookie_header_captured = FakeSignal()
        self.finished = FakeFinishedSignal()
        self.errors: list[str] = []
        self.shown = False
        self.closed = False
        self.raised = False
        self.activated = False

    def show_error(self, message: str) -> None:
        self.errors.append(message)

    def clear_error(self) -> None:
        self.errors.append("")

    def show(self) -> None:
        self.shown = True

    def close(self) -> bool:
        self.closed = True
        self.finished.emit(0)
        return True

    def raise_(self) -> None:
        self.raised = True

    def activateWindow(self) -> None:
        self.activated = True


class FakeLoginCoordinator:
    def __init__(
        self,
        fail: bool = False,
        restored_login: RestoredWebLogin | Exception | None = None,
    ) -> None:
        self.fail = fail
        self.restored_login = restored_login
        self.cookie_headers: list[str] = []
        self.restore_called = False

    def complete(self, login_result: Any) -> AuthSession:
        self.cookie_headers.append(login_result.cookie_header)
        if self.fail:
            raise RuntimeError("invalid login")
        return AuthSession(account_id="user-1", display_name="User One")

    def restore_last_session(self) -> RestoredWebLogin | None:
        self.restore_called = True
        if isinstance(self.restored_login, Exception):
            raise self.restored_login
        return self.restored_login


class FakeFileBrowser:
    def list_directory(self, parent_id: str = "0") -> list[WopanItem]:
        return [WopanItem(item_id="folder-1", name="Folder", kind=WopanItemKind.FOLDER)]

    def get_cloud_usage(self, account_id: str) -> WopanCloudUsage:
        return WopanCloudUsage(used_bytes=1024, total_bytes=2048)


@dataclass
class ControllerHarness:
    controller: ApplicationController
    main_window: MainWindow
    login_window: FakeLoginWindow
    coordinator: FakeLoginCoordinator
    quit_calls: list[str]


def _build_harness(
    fail_login: bool = False,
    restored_login: RestoredWebLogin | Exception | None = None,
    settings: AppSettings | None = None,
) -> ControllerHarness:
    login_window = FakeLoginWindow()
    coordinator = FakeLoginCoordinator(fail=fail_login, restored_login=restored_login)
    quit_calls: list[str] = []
    app_settings = settings or AppSettings()
    dependencies = AppDependencies(
        credential_store=CredentialStore(),
        web_login_coordinator=coordinator,  # type: ignore[arg-type]
        file_browser_factory=lambda _cookie, _settings: FakeFileBrowser(),  # type: ignore[arg-type]
        settings=app_settings,
    )
    main_window = MainWindow()
    controller = ApplicationController(
        dependencies,
        main_window,
        lambda: login_window,  # type: ignore[arg-type]
        lambda: quit_calls.append("quit"),
    )
    return ControllerHarness(controller, main_window, login_window, coordinator, quit_calls)


def test_controller_prompts_login_window(qapp: object) -> None:
    harness = _build_harness()

    harness.controller.prompt_login("登录已过期，请重新登录")

    assert harness.main_window.isVisible() is False
    assert harness.login_window.shown is True
    assert harness.login_window.raised is True
    assert harness.login_window.activated is True
    assert harness.login_window.errors[-1] == "登录已过期，请重新登录"


def test_controller_completes_login_and_attaches_file_browser(qapp: object) -> None:
    harness = _build_harness()
    token = "12345678-1234-1234-1234-123456789abc"

    harness.controller.prompt_login()
    harness.login_window.cookie_header_captured.emit(f"WoCloud-Web-Token=%22{token}%22")

    assert harness.coordinator.cookie_headers == [f"WoCloud-Web-Token=%22{token}%22"]
    assert harness.main_window.isVisible() is True
    assert harness.main_window.displayed_items()[0].name == "Folder"
    assert harness.login_window.closed is True
    assert harness.quit_calls == []


def test_controller_keeps_login_window_open_on_failure(qapp: object) -> None:
    harness = _build_harness(fail_login=True)
    token = "12345678-1234-1234-1234-123456789abc"

    harness.controller.prompt_login()
    harness.login_window.cookie_header_captured.emit(f"WoCloud-Web-Token=%22{token}%22")

    assert harness.main_window.displayed_items() == ()
    assert harness.login_window.closed is False
    assert harness.login_window.errors[-1] == "登录失败，请重试"


def test_controller_handles_invalid_cookie_without_sensitive_error(qapp: object) -> None:
    harness = _build_harness()

    harness.controller.prompt_login()
    harness.login_window.cookie_header_captured.emit("foo=bar")

    assert harness.coordinator.cookie_headers == []
    assert harness.login_window.errors[-1] == "登录失败，请重试"


def test_controller_restores_persisted_session_and_shows_main_window(qapp: object) -> None:
    cookie_header = "WoCloud-Web-Token=%22token-value%22"
    restored_login = RestoredWebLogin(
        session=AuthSession(account_id="user-1", display_name="User One"),
        cookie_header=cookie_header,
    )
    harness = _build_harness(restored_login=restored_login)

    harness.controller.start()

    assert harness.coordinator.restore_called is True
    assert harness.login_window.shown is False
    assert harness.main_window.isVisible() is True
    assert harness.main_window.displayed_items()[0].name == "Folder"
    assert harness.main_window.auth_session() == restored_login.session


def test_controller_skips_restore_when_stay_logged_in_disabled(qapp: object) -> None:
    cookie_header = "WoCloud-Web-Token=%22token-value%22"
    restored_login = RestoredWebLogin(
        session=AuthSession(account_id="user-1", display_name="User One"),
        cookie_header=cookie_header,
    )
    harness = _build_harness(
        restored_login=restored_login,
        settings=AppSettings(stay_logged_in=False),
    )

    harness.controller.start()

    assert harness.coordinator.restore_called is False
    assert harness.login_window.shown is True
    assert harness.main_window.isVisible() is False


def test_controller_prompts_login_when_restore_is_unavailable(qapp: object) -> None:
    harness = _build_harness(restored_login=None)

    harness.controller.start()

    assert harness.coordinator.restore_called is True
    assert harness.login_window.shown is True
    assert harness.main_window.isVisible() is False


def test_controller_quits_when_login_window_closes_before_login(qapp: object) -> None:
    harness = _build_harness()

    harness.controller.prompt_login()
    harness.login_window.finished.emit(0)

    assert harness.quit_calls == ["quit"]


def test_controller_logout_clears_session_and_prompts_login(qapp: object) -> None:
    cookie_header = "WoCloud-Web-Token=%22token-value%22"
    restored_login = RestoredWebLogin(
        session=AuthSession(account_id="user-1", display_name="User One"),
        cookie_header=cookie_header,
    )
    harness = _build_harness(restored_login=restored_login)

    harness.controller.start()
    harness.controller.logout()

    assert harness.main_window.auth_session() is None
    assert harness.main_window.isVisible() is False
    assert harness.login_window.shown is True


def test_controller_prompts_login_when_restore_raises(qapp: object) -> None:
    harness = _build_harness(restored_login=RuntimeError("corrupt credentials"))

    harness.controller.start()

    assert harness.login_window.shown is True
    assert harness.login_window.errors[-1] == "登录已过期，请重新登录"
    assert harness.main_window.isVisible() is False


def test_controller_prompts_login_when_file_browser_factory_fails(qapp: object) -> None:
    cookie_header = "WoCloud-Web-Token=%22token-value%22"
    restored_login = RestoredWebLogin(
        session=AuthSession(account_id="user-1"),
        cookie_header=cookie_header,
    )
    login_window = FakeLoginWindow()
    coordinator = FakeLoginCoordinator(restored_login=restored_login)
    quit_calls: list[str] = []
    dependencies = AppDependencies(
        credential_store=CredentialStore(),
        web_login_coordinator=coordinator,  # type: ignore[arg-type]
        file_browser_factory=_FailingFactory(),  # type: ignore[arg-type]
        settings=AppSettings(),
    )
    from openwopan.ui.main_window import MainWindow as _Window

    main_window = _Window()
    controller = ApplicationController(
        dependencies,
        main_window,
        lambda: login_window,  # type: ignore[arg-type]
        lambda: quit_calls.append("quit"),
    )

    controller.start()

    assert login_window.shown is True
    assert login_window.errors[-1] == "登录已过期，请重新登录"
    assert main_window.isVisible() is False


class _FailingFactory:
    def __call__(self, cookie_header: str, settings: object) -> object:
        raise RuntimeError("invalid cookie")


class _FailingCredentialStore(CredentialStore):
    def __init__(self) -> None:  # noqa: D107 - test double, no keyring access
        pass

    def delete_session_cookie(self, account_id: str) -> None:
        raise RuntimeError("keyring locked")

    def delete_last_account_id(self) -> None:
        raise RuntimeError("keyring locked")


def test_controller_logout_survives_credential_cleanup_failure(qapp: object) -> None:
    cookie_header = "WoCloud-Web-Token=%22token-value%22"
    restored_login = RestoredWebLogin(
        session=AuthSession(account_id="user-1"),
        cookie_header=cookie_header,
    )
    login_window = FakeLoginWindow()
    coordinator = FakeLoginCoordinator(restored_login=restored_login)
    dependencies = AppDependencies(
        credential_store=_FailingCredentialStore(),
        web_login_coordinator=coordinator,  # type: ignore[arg-type]
        file_browser_factory=lambda _cookie, _settings: FakeFileBrowser(),  # type: ignore[arg-type]
        settings=AppSettings(),
    )
    from openwopan.ui.main_window import MainWindow as _Window

    main_window = _Window()
    controller = ApplicationController(
        dependencies,
        main_window,
        lambda: login_window,  # type: ignore[arg-type]
        quit_application=lambda: None,
    )
    controller.start()

    controller.logout()

    assert main_window.auth_session() is None
    assert login_window.shown is True


def test_controller_logout_without_session_skips_cleanup(qapp: object) -> None:
    harness = _build_harness()

    harness.controller.logout()

    assert harness.main_window.auth_session() is None
    assert harness.login_window.shown is True


def test_controller_complete_login_without_prompted_window(qapp: object) -> None:
    harness = _build_harness()
    token = "12345678-1234-1234-1234-123456789abc"

    harness.controller.complete_login(f"WoCloud-Web-Token=%22{token}%22")

    assert harness.main_window.isVisible() is True
    assert harness.main_window.displayed_items()[0].name == "Folder"


def test_controller_login_window_finished_twice_quits_once(qapp: object) -> None:
    harness = _build_harness()

    harness.controller.prompt_login()
    harness.login_window.finished.emit(0)
    # second finished event arrives when no login window is tracked anymore
    harness.login_window.finished.emit(0)

    assert harness.quit_calls == ["quit"]


def test_controller_prompt_login_reuses_existing_window(qapp: object) -> None:
    harness = _build_harness()

    harness.controller.prompt_login()
    first_window = harness.login_window
    harness.controller.prompt_login("再次登录")

    assert harness.login_window is first_window
    assert harness.login_window.errors[-1] == "再次登录"
    assert harness.login_window.shown is True


def test_controller_complete_login_invalid_cookie_without_window(qapp: object) -> None:
    harness = _build_harness()

    harness.controller.complete_login("foo=bar")  # WebLoginError without a login window

    assert harness.main_window.displayed_items() == ()
    assert harness.login_window.errors == []  # window never created, no error shown


def test_controller_complete_login_factory_failure_without_window(qapp: object) -> None:
    harness = _build_harness()
    token = "12345678-1234-1234-1234-123456789abc"

    dependencies = AppDependencies(
        credential_store=CredentialStore(),
        web_login_coordinator=FakeLoginCoordinator(),  # type: ignore[arg-type]
        file_browser_factory=_FailingFactory(),  # type: ignore[arg-type]
        settings=AppSettings(),
    )
    from openwopan.ui.main_window import MainWindow as _Window

    controller = ApplicationController(
        dependencies,
        _Window(),
        lambda: FakeLoginWindow(),  # type: ignore[arg-type]
        quit_application=lambda: None,
    )

    controller.complete_login(f"WoCloud-Web-Token=%22{token}%22")  # factory raises

    assert controller._login_window is None


def test_controller_logout_deletes_credentials_on_success(qapp: object, monkeypatch: object) -> None:
    @dataclass
    class _LocalKeyring:
        values: dict[tuple[str, str], str]

        def set_password(self, service_name: str, key: str, value: str) -> None:
            self.values[(service_name, key)] = value

        def get_password(self, service_name: str, key: str) -> str | None:
            return self.values.get((service_name, key))

        def delete_password(self, service_name: str, key: str) -> None:
            self.values.pop((service_name, key), None)

    fake_keyring = _LocalKeyring(
        values={("openwopan", "wopan-session:last-account-id"): "user-1"}
    )
    monkeypatch.setattr("openwopan.storage.credentials.keyring", fake_keyring)  # type: ignore[attr-defined]
    cookie_header = "WoCloud-Web-Token=%22token-value%22"
    restored_login = RestoredWebLogin(
        session=AuthSession(account_id="user-1"),
        cookie_header=cookie_header,
    )
    harness = _build_harness(restored_login=restored_login)
    harness.controller.start()

    harness.controller.logout()

    assert ("svc", "wopan-session:last-account-id") not in fake_keyring.values
    assert harness.login_window.shown is True
