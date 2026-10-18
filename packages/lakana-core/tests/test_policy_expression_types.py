"""Tests for the type check of a row filter or column mask expression (#126)."""

import pytest
import sqlglot.expressions as exp

from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.errors import ValidationError
from lakana_core.policy.expression_types import _Checker, check_expression_types

COLUMNS = [
    ColumnSpec(name=name, type=declared, nullable=True)
    for name, declared in [
        ("id", "long"),
        ("n", "int"),
        ("code", "string"),
        ("email", "string"),
        ("amount", "decimal(10,2)"),
        ("score", "double"),
        ("active", "boolean"),
        ("born", "date"),
        ("seen", "timestamp"),
        ("Order Total", "double"),
        ("ref", "uuid"),
        ("tag", "struct<a: int>"),
    ]
]
"""One column of each type the check tells apart, and one of a type it does not know."""


def _refusal(expression: str, kind: str = "filter", masked: str | None = None) -> str:
    """Check an expression that must be refused and give the message.

    Parameters
    ----------
    expression
        The expression to check.
    kind
        `"filter"` or `"mask"`.
    masked
        The masked column, for a mask.

    Returns
    -------
    str
        The refusal's message.
    """
    with pytest.raises(ValidationError) as caught:
        check_expression_types(
            expression,
            kind="mask" if kind == "mask" else "filter",
            columns=COLUMNS,
            masked_column=masked,
        )
    return str(caught.value)


@pytest.mark.parametrize(
    ("expression", "fragment"),
    [
        ("code = 5", "`code` is text; compare it with '5', not 5."),
        ("code <> 5", "`code` is text; compare it with '5', not 5."),
        ("5 = code", "`code` is text; compare it with '5', not 5."),
        ("code >= 2.5", "`code` is text; compare it with '2.5', not 2.5."),
        ("code = -5", "`code` is text; compare it with '-5', not -5."),
        ("id = '5'", "`id` is long; compare it with 5, not '5'."),
        ("score < 'high'", "`score` is double; compare it with a number, not 'high'."),
        ("code = amount", "`code` is text but `amount` is decimal(10,2); compare columns"),
        ("id = code", "`id` is long but `code` is text; compare columns"),
        ("born = seen AND born = code", "`born` is date but `code` is text; compare columns"),
        ("born = 5", "`born` is date; compare it with a date such as DATE '2026-01-01', not 5."),
        ("born = '2026-01-01'", "`born` is date; compare it with DATE '2026-01-01', not"),
        (
            "seen > '2026-01-01 00:00:00'",
            "`seen` is timestamp; compare it with TIMESTAMP '2026-01-01 00:00:00', not",
        ),
        ("active = 1", "`active` is boolean; compare it with TRUE or FALSE, not 1."),
        ("active = 'yes'", "`active` is boolean; compare it with TRUE or FALSE, not 'yes'."),
        ("ref = 'abc'", "`ref` is uuid; it can only be compared with another uuid column"),
        ("current_user() = 5", "current_user() is text; compare it with '5', not 5."),
        ("code = current_user() AND id = current_user()", "`id` is long but current_user() is"),
        ("(id = 1) AND (code = 5)", "`code` is text; compare it with '5', not 5."),
        ("NOT (code = 5)", "`code` is text; compare it with '5', not 5."),
        ("id = 1 OR (n = 2 AND code = 5)", "`code` is text; compare it with '5', not 5."),
        ("\"Order Total\" > 'x'", "`Order Total` is double; compare it with a number, not 'x'."),
        ("tag = 1", "the type of `tag` is struct<a: int>, which a policy cannot compare"),
        ("'a' = 1", "the text 'a' is text; compare it with '1', not 1."),
        ("code", "`code` is text, not boolean; a condition is needed here."),
        ("id", "`id` is long, not boolean; a condition is needed here."),
        ("code AND active", "`code` is text, not boolean; a condition is needed here."),
        ("active OR amount", "`amount` is decimal(10,2), not boolean"),
        ("NOT code", "`code` is text, not boolean"),
        ("1", "the number 1 is not a condition"),
        ("'a'", "the text 'a' is not a condition"),
        ("-code = 1", "`code` is text, not a number; a minus sign needs a number."),
        ("tag", "the type of `tag` is struct<a: int>, which a policy cannot compare"),
        ("((code))", "`code` is text, not boolean"),
        ("-5 = 'x'", "the number -5 is"),
        ("-id = TRUE", "-id is long; compare it with a number, not TRUE."),
    ],
)
def test_a_row_filter_that_needs_an_implicit_cast_is_refused(
    expression: str, fragment: str
) -> None:
    """Each refused shape names the column, both types and the fix."""
    message = _refusal(expression)
    assert fragment in message
    assert " is not a valid filter expression: " in message


@pytest.mark.parametrize(
    "expression",
    [
        "id = 5",
        "id = n",
        "n = id",
        "id = amount",
        "amount = score",
        "score > 1.5",
        "n <= -3",
        "amount <> 0",
        "id >= 1e3",
        "code = 'x'",
        "code <> current_user()",
        "current_user() = 'alice'",
        "code = ''",
        "code = NULL",
        "NULL = code",
        "born = NULL",
        "born = DATE '2026-01-01'",
        "born >= DATE '2026-01-01' AND born < DATE '2027-01-01'",
        "seen >= TIMESTAMP '2026-01-01 00:00:00'",
        "seen < TIMESTAMP '2026-01-01 00:00:00.250'",
        "seen < TIMESTAMP '2026-01-01'",
        "born < seen",
        "seen > DATE '2026-01-01'",
        "active",
        "NOT active",
        "active = TRUE",
        "active <> FALSE",
        "active AND id > 1",
        "has_group('staff')",
        "has_group('staff') OR code = 'x'",
        "TRUE",
        "FALSE",
        "NULL",
        "(id = 1) AND (code = 'x')",
        "NOT (id = 1)",
        "(active)",
        "id = -5",
        "(id = 1) = (n = 2)",
        '"Order Total" > 10',
        '"Order Total" > id',
        "CODE = 'x'",
        "1 = 1",
        "ref = ref",
    ],
)
def test_a_row_filter_that_compares_like_with_like_is_accepted(expression: str) -> None:
    """Every allowed shape passes."""
    check_expression_types(expression, kind="filter", columns=COLUMNS)


def test_a_filter_that_reads_no_column_is_checked_without_a_table() -> None:
    """A constant needs no schema."""
    check_expression_types("has_group('a') AND 1 = 1", kind="filter", columns=[])
    with pytest.raises(ValidationError, match="the number 1 is not a condition"):
        check_expression_types("1", kind="filter", columns=[])


@pytest.mark.parametrize(
    "expression",
    [
        "born = DATE 'banana'",
        "born = DATE '2026-13-01'",
        "born = DATE '20260101'",
        "born = DATE '2026-1-1'",
        "seen = TIMESTAMP 'banana'",
        "seen = TIMESTAMP '2026-01-01T00:00:00'",
        "seen = TIMESTAMPTZ '2026-01-01 00:00:00+00'",
        "seen = TIMESTAMP '2026-01-01 00:00'",
        "seen = TIMESTAMP '2026-01-01 00:00:00.1234567'",
        "seen = TIMESTAMP '2026-01-01 00:00:00.12x'",
    ],
)
def test_a_typed_literal_that_is_not_a_valid_date_is_refused_at_creation(
    expression: str,
) -> None:
    """A typed literal that DuckDB could fail on is refused when the policy is made."""
    message = _refusal(expression)
    assert "is not a valid expression" in message
    assert "DATE 'YYYY-MM-DD'" in message or "TIMESTAMP 'YYYY-MM-DD HH:MM:SS'" in message


@pytest.mark.parametrize(
    "expression",
    ["CAST(code AS INT)", "CAST('5' AS INT)", "TRY_CAST(code AS DATE)", "id = code::INT"],
)
def test_no_other_cast_is_allowed(expression: str) -> None:
    """Only a typed date or timestamp literal is a conversion; the rest stays refused."""
    assert "outside the restricted subset" in _refusal(expression)


@pytest.mark.parametrize(
    ("column", "expression"),
    [
        ("id", "NULL"),
        ("id", "0"),
        ("id", "-1"),
        ("n", "0"),
        ("score", "0"),
        ("score", "1.5"),
        ("amount", "0"),
        ("code", "'***'"),
        ("code", "NULL"),
        ("code", "''"),
        ("email", "substr(email, 1, 2) || '…'"),
        ("email", "substring(email, 3)"),
        ("email", "md5(email)"),
        ("email", "current_user()"),
        ("email", "code || '-' || email"),
        ("email", "email"),
        ("id", "id"),
        ("born", "NULL"),
        ("born", "DATE '1900-01-01'"),
        ("seen", "TIMESTAMP '1900-01-01 00:00:00'"),
        ("active", "FALSE"),
        ("active", "NULL"),
        ("active", "id = 1"),
        ("active", "has_group('staff')"),
        ("active", "NOT active"),
        ("tag", "NULL"),
        ("tag", "tag"),
    ],
)
def test_a_mask_of_the_columns_own_type_is_accepted(column: str, expression: str) -> None:
    """A mask keeps the column's type: a value of it, `NULL`, or a type-keeping function."""
    check_expression_types(expression, kind="mask", columns=COLUMNS, masked_column=column)


@pytest.mark.parametrize(
    ("column", "expression", "fragment"),
    [
        (
            "id",
            "'x'",
            "`id` is long, but the mask is text. Use a value of its own type: 0 or NULL.",
        ),
        ("n", "'0'", "`n` is int, but the mask is text. Use a value of its own type: 0 or NULL."),
        ("id", "1.5", "`id` is long, but the mask is a decimal number. Use a value of its own"),
        ("id", "md5(code)", "`id` is long, but the mask is text."),
        (
            "code",
            "0",
            "`code` is text, but the mask is a number. Use a value of its own type: '***'",
        ),
        ("code", "id = 1", "`code` is text, but the mask is boolean."),
        (
            "born",
            "'1900-01-01'",
            "`born` is date, but the mask is text. Use a value of its own type:",
        ),
        ("born", "TIMESTAMP '1900-01-01 00:00:00'", "`born` is date, but the mask is timestamp."),
        ("seen", "DATE '1900-01-01'", "`seen` is timestamp, but the mask is date."),
        ("active", "0", "`active` is boolean, but the mask is a number. Use a value of its own"),
        ("active", "'no'", "`active` is boolean, but the mask is text."),
        ("tag", "0", "the type of `tag` is struct<a: int>, which a mask cannot be checked against"),
        ("tag", "'x'", "the type of `tag` is struct<a: int>, which a mask cannot be checked"),
        ("email", "email || id", "`email` is text but `id` is long; `||` joins text"),
        ("email", "substr(id, 1, 2)", "`id` is long, not text; substr needs text"),
        ("email", "substr(email, 'a')", "'a' is text, not a number; substr needs a number"),
        ("email", "md5(id)", "`id` is long, not text; md5 needs text"),
        ("id", "0 AND 1", "the number 0 is not a condition"),
    ],
)
def test_a_mask_of_another_type_is_refused(column: str, expression: str, fragment: str) -> None:
    """A mask that would change the column's type names both types and a value that keeps it."""
    message = _refusal(expression, kind="mask", masked=column)
    assert fragment in message
    assert " is not a valid mask expression: " in message


def test_a_mask_is_checked_for_cross_type_comparisons_too() -> None:
    """A comparison inside a mask follows the same rule as in a filter."""
    message = _refusal("code = 5", kind="mask", masked="active")
    assert "`code` is text; compare it with '5', not 5." in message


def test_the_message_never_carries_more_than_the_expression_shown_safely() -> None:
    """A control character in a column name is escaped in the message."""
    columns = [ColumnSpec(name="a\nb", type="string", nullable=True)]
    with pytest.raises(ValidationError) as caught:
        check_expression_types('"a\nb" = 5', kind="filter", columns=columns)
    assert "\n" not in str(caught.value)


def test_a_column_missing_from_the_table_is_not_this_checks_business() -> None:
    """An unknown column is refused with the type unknown, so no policy slips through."""
    message = _refusal("ghost = 5")
    assert "the type of `ghost` is not known" in message


@pytest.mark.parametrize(
    ("declared", "family"),
    [
        ("int", "integer"),
        ("INTEGER", "integer"),
        ("BIGINT", "integer"),
        ("smallint", "integer"),
        ("float", "fractional"),
        ("REAL", "fractional"),
        ("DECIMAL(18,3)", "fractional"),
        ("VARCHAR", "text"),
        ("varchar(20)", "text"),
        ("TIMESTAMP", "timestamp"),
        ("timestamptz", "timestamptz"),
        ("bool", "boolean"),
    ],
)
def test_the_type_names_of_an_external_table_are_understood(declared: str, family: str) -> None:
    """An external table's inferred type names are read like Iceberg's."""
    columns = [ColumnSpec(name="c", type=declared, nullable=True)]
    comparable = {
        "integer": "c = 1",
        "fractional": "c > 1.5",
        "text": "c = 'x'",
        "timestamp": "c > TIMESTAMP '2026-01-01 00:00:00'",
        "timestamptz": "c > TIMESTAMP '2026-01-01 00:00:00'",
        "boolean": "c = TRUE",
    }[family]
    check_expression_types(comparable, kind="filter", columns=columns)


def test_a_function_of_the_wrong_type_used_as_a_condition_is_refused() -> None:
    """A text function where a condition is needed names the function and its type."""
    message = _refusal("0 = 0 AND md5(code)", kind="mask", masked="active")
    assert "MD5(code) is text, not boolean; a condition is needed here." in message


def test_a_function_result_compared_with_a_number_is_refused() -> None:
    """The subject of a mismatch can be a function, not only a column."""
    message = _refusal("md5(code) = 5", kind="mask", masked="active")
    assert "MD5(code) is text; compare it with '5', not 5." in message


def test_a_mask_whose_column_is_not_in_the_table_cannot_be_checked() -> None:
    """The service checks the column first; the check refuses rather than guess."""
    message = _refusal("NULL", kind="mask", masked="ghost")
    assert "the type of the masked column `ghost` is not known" in message
    assert "the type of the masked column `` is not known" in _refusal("1", kind="mask")


def test_a_node_without_a_rule_is_refused_not_guessed() -> None:
    """A part of the tree the check has no rule for fails closed."""
    checker = _Checker("x", "filter", COLUMNS)
    with pytest.raises(ValidationError, match="could not be determined"):
        checker.type_of(exp.Identifier(this="x"))
