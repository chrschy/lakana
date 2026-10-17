"""The fixed message of a failed read under a row filter or column mask."""

from __future__ import annotations

from lakana_core.policy.read_errors import is_policy_read_failure, policy_read_failure
from lakana_core.runs.checks import error_text


def test_the_message_names_the_table_and_says_what_to_do() -> None:
    """A table is named in backticks, escaped; without one the message says "one of the tables"."""
    named = policy_read_failure("gold.people")
    unnamed = policy_read_failure(None)

    assert "under a row filter or column mask on `gold.people`; the value is not shown." in named
    assert "on one of the tables; the value is not shown." in unnamed
    assert "try_cast or TRY" in named
    assert "`gold.pe\\nple`" in policy_read_failure("gold.pe\nple")


def test_only_the_message_is_recognised_as_the_message() -> None:
    """The engine's own text, an empty text and no text are not."""
    assert is_policy_read_failure(policy_read_failure("gold.people"))
    assert is_policy_read_failure(policy_read_failure(None))
    assert not is_policy_read_failure("Query failed: Conversion Error: Could not convert")
    assert not is_policy_read_failure(None)


def test_a_check_error_keeps_the_message_and_cuts_the_engines_text() -> None:
    """The fixed message holds no value and shows as it is; the engine's sentence is cut."""
    fixed = policy_read_failure("gold.people")

    assert error_text("invalid_query", fixed, "runner") == fixed
    assert error_text(
        "invalid_query", "Query failed: Conversion Error: Could not convert 'x'", "runner"
    ).startswith("Conversion Error: the check's query failed")
