"""Every place that decrypts a secret checks the secret's use first."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_PACKAGES = Path(__file__).resolve().parents[2]
_SITES = {
    "lakana-core/src/lakana_core/storage/resolver.py": "_resolve_secret_values",
    "lakana-worker/src/lakana_worker/deploy.py": "resolve_credential",
    "lakana-worker/src/lakana_worker/index_credentials.py": "resolve_index_access",
    "lakana-worker/src/lakana_worker/notify.py": "_webhook",
}
_CRYPTO = "lakana-core/src/lakana_core/secrets/crypto.py"


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    """Find the calls of a function by its name.

    Parameters
    ----------
    node
        The tree to search.
    name
        The called function's name, as a bare name or an attribute.

    Returns
    -------
    list of ast.Call
        The calls, in source order.
    """
    found = [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and name in (getattr(call.func, "id", None), getattr(call.func, "attr", None))
    ]
    return sorted(found, key=lambda call: (call.lineno, call.col_offset))


def _under_broad_handler(tree: ast.AST, call: ast.Call) -> bool:
    """Say whether a call sits in a `try` body whose handler catches every exception.

    Parameters
    ----------
    tree
        The function's tree.
    call
        The call.

    Returns
    -------
    bool
        Whether a bare `except` or `except Exception` could swallow or relabel what the call
        raises (the refusal and the environment-mismatch `ValueError` must reach the caller).
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not any(call in ast.walk(stmt) for stmt in node.body):
            continue
        for handler in node.handlers:
            kind = handler.type
            if kind is None or getattr(kind, "id", None) in {"Exception", "BaseException"}:
                return True
    return False


def test_only_the_four_known_sites_decrypt_a_secret() -> None:
    """A fifth caller of the decrypt functions would read a secret for any use."""
    callers = sorted(
        str(path.relative_to(_PACKAGES))
        for path in _PACKAGES.glob("*/src/**/*.py")
        if "decrypt_secret_row(" in path.read_text(encoding="utf-8")
        and str(path.relative_to(_PACKAGES)) != _CRYPTO
    )

    assert callers == sorted(_SITES)


@pytest.mark.parametrize(("path", "function"), sorted(_SITES.items()))
def test_each_decrypt_site_requires_the_use_before_it_decrypts(path: str, function: str) -> None:
    """`require_use` runs first and outside any broad `except`, so its refusal is not swallowed."""
    tree = ast.parse((_PACKAGES / path).read_text(encoding="utf-8"))
    (site,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == function
    ]

    required, decrypted = _calls(site, "require_use"), _calls(site, "decrypt_secret_row")

    assert len(decrypted) == 1
    assert len(required) >= 1
    assert required[0].lineno < decrypted[0].lineno
    assert not any(_under_broad_handler(site, call) for call in required)
