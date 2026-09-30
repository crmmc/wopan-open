"""Windows-compatibility contract for the test conftest (backlog B25).

``resource`` is a POSIX-only stdlib module. A module-level ``import resource``
in ``tests/conftest.py`` crashed every Windows pytest session at collection —
before a single test ran — and CI (ubuntu-only) cannot catch it, so the
contract is enforced by a test instead.

Runtime introspection (``sys.modules`` membership) is order-dependent and
flaky under a real pytest run, so the contract is asserted on the parsed AST:
it is deterministic and independent of the interpreter's import state. Both
acceptable fix shapes pass it — a lazy import inside the hook, or a
module-level ``try: import resource / except ImportError`` guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

_CONFTEST = Path(__file__).with_name("conftest.py")


def _conftest_module() -> ast.Module:
    return ast.parse(_CONFTEST.read_text(encoding="utf-8"), filename=str(_CONFTEST))


def _imported_roots(node: ast.stmt) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[0] for alias in node.names}
    return set()


def test_conftest_imports_no_resource_module_at_top_level() -> None:
    tree = _conftest_module()
    top_level_imports: set[str] = set()
    for node in tree.body:
        top_level_imports |= _imported_roots(node)
    assert "resource" not in top_level_imports


def test_resource_import_is_guarded_against_import_error() -> None:
    tree = _conftest_module()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        imports_resource = any(
            isinstance(stmt, ast.Import)
            and any(alias.name.split(".")[0] == "resource" for alias in stmt.names)
            for stmt in node.body
        )
        guards_import_error = any(
            isinstance(handler.type, ast.Name) and handler.type.id == "ImportError"
            for handler in node.handlers
        )
        if imports_resource and guards_import_error:
            break
    else:
        raise AssertionError(
            "tests/conftest.py must import 'resource' inside a "
            "try/except ImportError guard (POSIX-only module)"
        )
