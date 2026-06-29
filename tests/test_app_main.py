from __future__ import annotations

from openwopan.app.main import _application_args


def test_application_args_preserves_explicit_args() -> None:
    assert _application_args(["openwopan", "--flag"]) == ["openwopan", "--flag"]


def test_application_args_supplies_program_name_for_empty_args() -> None:
    assert _application_args([]) == ["openwopan"]
