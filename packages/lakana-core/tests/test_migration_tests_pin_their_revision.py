"""A migration's own Postgres test names its revision and never ``head``.

``head`` moves with the next migration. A test that upgrades to ``head`` and then asserts
its own revision passes until a later migration is added, and then fails for a reason that
has nothing to do with that migration. This module reads the source of the integration tests
and needs no database.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_TESTS = Path(__file__).with_name("test_migrations_postgres_integration.py")
_OWN_TEST = re.compile(r"^test_(\d{4})_")


def _is_version_read(node: ast.expr) -> bool:
    """Say whether an expression reads the database's migration revision.

    Parameters
    ----------
    node
        The expression.

    Returns
    -------
    bool
        `True` for a call of ``_alembic_version``.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_alembic_version"
    )


def _target_of(call: ast.Call) -> str | None:
    """Read the revision a ``migrate.upgrade`` or ``migrate.downgrade`` call moves to.

    Parameters
    ----------
    call
        The call.

    Returns
    -------
    str or None
        The revision as written, from the second positional argument or the ``revision``
        keyword; `None` when the call names none.
    """
    if len(call.args) >= 2:
        return ast.unparse(call.args[1])
    for keyword in call.keywords:
        if keyword.arg == "revision":
            return ast.unparse(keyword.value)
    return None


def _problems(source: str) -> list[str]:
    """Find the places where a migration test depends on what ``head`` is.

    Two rules. In every test function, an ``assert _alembic_version(...) == X`` must follow a
    ``migrate.upgrade`` or ``migrate.downgrade`` whose target is written exactly as ``X``. In
    a function named ``test_NNNN_...``, which is the test of migration ``NNNN``, no upgrade
    names ``"head"``, and one upgrade names a variable that ends in ``_NNNN``.

    Parameters
    ----------
    source
        The source of a module of migration tests.

    Returns
    -------
    list of str
        One line for each problem, with the function and the line; empty when there is none.
    """
    found: list[str] = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not function.name.startswith("test_"):
            continue
        moves: list[tuple[int, str, str]] = []
        asserted: list[tuple[int, str]] = []
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "migrate"
                and node.func.attr in ("upgrade", "downgrade")
            ):
                target = _target_of(node)
                if target is not None:
                    moves.append((node.lineno, node.func.attr, target))
            if (
                isinstance(node, ast.Assert)
                and isinstance(node.test, ast.Compare)
                and _is_version_read(node.test.left)
                and len(node.test.ops) == 1
                and isinstance(node.test.ops[0], ast.Eq)
            ):
                asserted.append((node.lineno, ast.unparse(node.test.comparators[0])))
        for line, revision in asserted:
            before = [target for at, _, target in sorted(moves) if at < line]
            if not before or before[-1] != revision:
                found.append(
                    f"{function.name}, line {line}: asserts the revision {revision} after a "
                    f"move to {before[-1] if before else 'nothing'}; move to {revision} itself."
                )
        own = _OWN_TEST.match(function.name)
        if own is None:
            continue
        upgrades = [(at, target) for at, kind, target in sorted(moves) if kind == "upgrade"]
        found.extend(
            f'{function.name}, line {at}: upgrades to "head", which the next migration moves; '
            "name the migration's own revision."
            for at, target in upgrades
            if target == "'head'"
        )
        if not any(target.endswith(f"_{own.group(1)}") for _, target in upgrades):
            found.append(
                f"{function.name}: never upgrades to a variable named for revision {own.group(1)}."
            )
    return found


def test_every_migrations_own_test_pins_its_revision() -> None:
    """The tests of single migrations on Postgres do not depend on what ``head`` is.

    Both rules hold for every test named ``test_NNNN_...``, the older ones included, and for
    methods, async tests and calls that name the revision by keyword.
    """
    assert _problems(_TESTS.read_text()) == []


def test_the_rules_find_a_test_that_depends_on_head() -> None:
    """A small module with each slip, and one without any."""
    slips = """
def test_0030_on_postgres():
    revision_0029 = "a"
    revision_0030 = "b"
    migrate.upgrade(url, revision_0029)
    migrate.upgrade(url, "head")
    assert _alembic_version(engine) == revision_0030
    migrate.downgrade(url, revision_0029)
    assert _alembic_version(engine) == revision_0029


def test_0031_round_trips_on_postgres():
    migrate.upgrade(url, "head")
    assert _alembic_version(engine) == revision_0031
    assert _alembic_version(engine) != revision_0030
"""
    assert _problems(slips) == [
        "test_0030_on_postgres, line 7: asserts the revision revision_0030 after a move to "
        "'head'; move to revision_0030 itself.",
        'test_0030_on_postgres, line 6: upgrades to "head", which the next migration moves; '
        "name the migration's own revision.",
        "test_0030_on_postgres: never upgrades to a variable named for revision 0030.",
        "test_0031_round_trips_on_postgres, line 14: asserts the revision revision_0031 after "
        "a move to 'head'; move to revision_0031 itself.",
        'test_0031_round_trips_on_postgres, line 13: upgrades to "head", which the next '
        "migration moves; name the migration's own revision.",
        "test_0031_round_trips_on_postgres: never upgrades to a variable named for revision 0031.",
    ]
    older = """
def test_0014_round_trips_on_postgres():
    migrate.upgrade(url, "head")


class TestOthers:
    def test_0040_on_postgres(self):
        migrate.upgrade(url=url, revision="head")
        assert _alembic_version(engine) == revision_0040


async def test_0041_on_postgres():
    migrate.upgrade(url, "head")
"""
    assert _problems(older) == [
        'test_0014_round_trips_on_postgres, line 3: upgrades to "head", which the next '
        "migration moves; name the migration's own revision.",
        "test_0014_round_trips_on_postgres: never upgrades to a variable named for revision 0014.",
        'test_0041_on_postgres, line 13: upgrades to "head", which the next migration moves; '
        "name the migration's own revision.",
        "test_0041_on_postgres: never upgrades to a variable named for revision 0041.",
        "test_0040_on_postgres, line 9: asserts the revision revision_0040 after a move to "
        "'head'; move to revision_0040 itself.",
        'test_0040_on_postgres, line 8: upgrades to "head", which the next migration moves; '
        "name the migration's own revision.",
        "test_0040_on_postgres: never upgrades to a variable named for revision 0040.",
    ]
    sound = """
def test_0030_on_postgres():
    migrate.upgrade(url, revision_0030)
    assert _alembic_version(engine) == revision_0030


def test_a_helper_style_round_trip_on_postgres():
    migrate.upgrade(url, "head")
    migrate.downgrade(url, revision_0028)
    assert _alembic_version(engine) == revision_0028


def helper():
    migrate.upgrade(url, "head")
    assert _alembic_version(engine) == anything
"""
    assert _problems(sound) == []
