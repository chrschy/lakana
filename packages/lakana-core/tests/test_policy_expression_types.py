"""Tests for the type check of a row filter or column mask expression (#126)."""

from decimal import Decimal

import pyarrow as pa
import pytest
import sqlglot.expressions as exp

from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.errors import ValidationError
from lakana_core.policy.expression_types import (
    _Checker,
    _column_type,
    _rule_for,
    _Type,
    check_expression_types,
)

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
        ("seen_tz", "timestamptz"),
        ("small", "smallint"),
        ("flt", "float"),
        ("dec38", "decimal(38,0)"),
        ("amount2", "decimal(10,2)"),
        ("amount3", "decimal(12,2)"),
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
        ("-code = 1", "A minus sign can only be written in front of a number such as -5."),
        ("tag", "the type of `tag` is struct<a: int>, which a policy cannot compare"),
        ("((code))", "`code` is text, not boolean"),
        ("-5 = 'x'", "the number -5 is"),
        ("-id = 5", "A minus sign can only be written in front of a number such as -5."),
        ("-5 = TRUE", "the number -5 is a number"),
        (
            "id = amount",
            "`id` is long but `amount` is decimal(10,2); compare columns of the same kind",
        ),
        (
            "amount = score",
            "`amount` is decimal(10,2) but `score` is double; compare columns of the same kind",
        ),
        ("id = score", "`id` is long but `score` is double; compare columns of the same kind"),
        ("n = flt", "`n` is int but `flt` is float; compare columns of the same kind"),
        (
            "amount = amount3",
            "`amount` is decimal(10,2) but `amount3` is decimal(12,2); compare columns of "
            "the same kind",
        ),
        ("dec38 = amount", "`dec38` is decimal(38,0) but `amount` is decimal(10,2)"),
        ("id = 1.5", "`id` is long; compare it with a whole number such as 5, not 1.5."),
        ("id = 1e3", "`id` is long; compare it with a whole number such as 5, not 1e3."),
        (
            "n = 99999999999",
            "`n` is int; compare it with a value from -2147483648 to 2147483647, not 99999999999.",
        ),
        ("n = -2147483649", "from -2147483648 to 2147483647"),
        ("small = 40000", "from -32768 to 32767"),
        ("id = 99999999999999999999", "from -9223372036854775808 to 9223372036854775807"),
        (
            "amount = 1.234",
            "`amount` is decimal(10,2); compare it with a number with at most 2 decimal "
            "places and 8 digits before the point, not 1.234.",
        ),
        (
            "amount = 123456789",
            "a number with at most 2 decimal places and 8 digits before the point",
        ),
        ("dec38 = 1.5", "a number with at most 0 decimal places and 38 digits before the point"),
        (
            "amount = 1e3",
            "`amount` is decimal(10,2); compare it with a number written without an "
            "exponent, not 1e3.",
        ),
        (
            "flt = 1e999",
            "`flt` is float; compare it with a number from -3.4028235e+38 to "
            "3.4028235e+38, not 1e999.",
        ),
        ("score = 1e999", "from -1.7976931348623157e+308 to 1.7976931348623157e+308"),
        ("code = NULL", "comparing with NULL never matches a row"),
        ("NULL = code", "comparing with NULL never matches a row"),
        ("born = NULL", "comparing with NULL never matches a row"),
        (
            "seen_tz > TIMESTAMP '2026-01-01 00:00:00'",
            "`seen_tz` is timestamptz; the read does not fix a time zone",
        ),
        ("seen_tz < DATE '2026-01-01'", "the read does not fix a time zone"),
        ("born < seen_tz", "the read does not fix a time zone"),
        ("seen_tz = seen", "the read does not fix a time zone"),
        ("seen_tz = 5", "`seen_tz` is timestamptz"),
        ("(seen_tz) = 'x'", "`seen_tz` is timestamptz"),
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
        "amount = amount2",
        "score = flt",
        "small = n",
        "id = small",
        "seen_tz = seen_tz",
        "score > 1.5",
        "n <= -3",
        "amount <> 0",
        "n = -2147483648",
        "n = 2147483647",
        "small = -32768",
        "id = 9223372036854775807",
        "amount = 99999999.99",
        "amount = 1.5",
        "amount = -0.01",
        "amount = 0",
        "dec38 = 12345678901234567890123456789012345678",
        "flt = 1e38",
        "score = 1e300",
        "score = 5",
        "flt = 1.5",
        "born = DATE '0999-01-01'",
        "code = 'x'",
        "code <> current_user()",
        "current_user() = 'alice'",
        "code = ''",
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
        '"Order Total" > score',
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
        ("id", "n"),
        ("id", "small"),
        ("n", "small"),
        ("n", "2147483647"),
        ("score", "flt"),
        ("score", "1e300"),
        ("flt", "1.5"),
        ("amount", "amount2"),
        ("amount", "1.25"),
        ("amount", "99999999.99"),
        ("amount", "-0.01"),
        ("amount3", "amount"),
        ("Order Total", "score"),
        ("dec38", "n"),
        ("seen_tz", "NULL"),
        ("seen_tz", "seen_tz"),
        ("ref", "ref"),
        ("ref", "NULL"),
        ("born", "born"),
        ("seen", "seen"),
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
        ("email", "substr(email, 'a')", "substr needs a whole number such as substr(email, 1, 2)"),
        ("email", "substr(email, 1.5)", "substr needs a whole number"),
        ("email", "substr(email, 1e30)", "substr needs a whole number"),
        ("email", "substr(email, score)", "substr needs a whole number"),
        ("email", "substr(email, id)", "substr needs a whole number"),
        ("email", "substr(email, 1, small)", "substr needs a whole number"),
        ("email", "substr(email, 99999999999999999999)", "substr needs a whole number"),
        ("email", "substr(email, 2147483648)", "from -2147483648 to 2147483647"),
        ("email", "substr(email, 1, -2147483649)", "from -2147483648 to 2147483647"),
        ("n", "id", "`n` is int, but the mask `id` is long and may not fit"),
        ("n", "99999999999", "`n` is int, but the mask 99999999999 may not fit"),
        (
            "id",
            "99999999999999999999",
            "`id` is long, but the mask 99999999999999999999 may not fit",
        ),
        ("small", "n", "`small` is smallint, but the mask `n` is int and may not fit"),
        ("score", "id", "`score` is double, but the mask `id` is long and may not fit"),
        ("flt", "score", "`flt` is float, but the mask `score` is double and may not fit"),
        (
            "amount",
            "score",
            "`amount` is decimal(10,2), but the mask `score` is double and may not fit",
        ),
        ("amount", "1.234", "`amount` is decimal(10,2), but the mask 1.234 may not fit"),
        ("amount", "id", "`amount` is decimal(10,2), but the mask `id` is long and may not fit"),
        (
            "amount",
            "amount3",
            "`amount` is decimal(10,2), but the mask `amount3` is decimal(12,2) and may not fit",
        ),
        ("amount", "1e3", "`amount` is decimal(10,2), but the mask 1e3 may not fit"),
        (
            "seen_tz",
            "TIMESTAMP '1900-01-01 00:00:00'",
            "`seen_tz` is timestamptz, but the mask is timestamp",
        ),
        ("seen_tz", "seen", "`seen_tz` is timestamptz, but the mask is timestamp"),
        ("ref", "'x'", "`ref` is uuid, but the mask is text"),
        ("ref", "born", "`ref` is uuid, but the mask is date"),
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


def _stored(arrow_type: pa.DataType) -> str:
    """Spell an Arrow type the way `infer_schema` stores it.

    Parameters
    ----------
    arrow_type
        A real pyarrow type.

    Returns
    -------
    str
        `str(arrow_type)`, which is what an external table's schema holds.
    """
    return str(arrow_type)


_ARROW_FILTERS = [
    (pa.int8(), "c = 5"),
    (pa.int16(), "c = 5"),
    (pa.int32(), "c = 5"),
    (pa.int64(), "c = 5"),
    (pa.float32(), "c > 1.5"),
    (pa.float64(), "c > 1.5"),
    (pa.decimal128(10, 2), "c > 1.5"),
    (pa.string(), "c = 'x'"),
    (pa.large_string(), "c = 'x'"),
    (pa.bool_(), "c = TRUE"),
    (pa.date32(), "c > DATE '2026-01-01'"),
    (pa.timestamp("us"), "c > TIMESTAMP '2026-01-01 00:00:00'"),
    (pa.timestamp("us", tz="UTC"), "c = c"),
]


@pytest.mark.parametrize(("arrow_type", "expression"), _ARROW_FILTERS)
def test_the_types_an_external_table_stores_are_understood_in_a_filter(
    arrow_type: pa.DataType, expression: str
) -> None:
    """The names `infer_schema` really stores (`int64`, `date32[day]`, ...) type-check."""
    columns = [ColumnSpec(name="c", type=_stored(arrow_type), nullable=True)]
    check_expression_types(expression, kind="filter", columns=columns)


@pytest.mark.parametrize(
    ("arrow_type", "expression", "fragment"),
    [
        (pa.int64(), "c = 'x'", "`c` is int64; compare it with a number, not 'x'."),
        (pa.large_string(), "c = 5", "`c` is text; compare it with '5', not 5."),
        (pa.date32(), "c = 5", "`c` is date32[day]; compare it with a date such as DATE"),
        (pa.decimal128(10, 2), "c = 1.234", "`c` is decimal128(10, 2); compare it with a number"),
        (pa.int64(), "c = 99999999999999999999", "from -9223372036854775808"),
    ],
)
def test_a_mismatch_on_an_external_table_names_the_stored_type(
    arrow_type: pa.DataType, expression: str, fragment: str
) -> None:
    """The message names a type the user can see in the table's schema."""
    columns = [ColumnSpec(name="c", type=_stored(arrow_type), nullable=True)]
    with pytest.raises(ValidationError) as caught:
        check_expression_types(expression, kind="filter", columns=columns)
    assert fragment in str(caught.value)


@pytest.mark.parametrize(
    ("arrow_type", "mask"),
    [
        (pa.int64(), "0"),
        (pa.decimal128(10, 2), "1.25"),
        (pa.large_string(), "'***'"),
        (pa.date32(), "DATE '1970-01-01'"),
        (pa.timestamp("us"), "TIMESTAMP '1970-01-01 00:00:00'"),
        (pa.bool_(), "FALSE"),
    ],
)
def test_a_constant_mask_of_the_columns_own_type_is_accepted_on_an_external_table(
    arrow_type: pa.DataType, mask: str
) -> None:
    """The mask page promises constants work on external tables too."""
    columns = [ColumnSpec(name="c", type=_stored(arrow_type), nullable=True)]
    check_expression_types(mask, kind="mask", columns=columns, masked_column="c")


@pytest.mark.parametrize(
    "arrow_type",
    [pa.uint8(), pa.uint64()],
)
def test_an_arrow_type_the_check_has_no_rule_for_names_the_cause(arrow_type: pa.DataType) -> None:
    """Fail closed, and say which stored types are not supported yet."""
    columns = [ColumnSpec(name="c", type=_stored(arrow_type), nullable=True)]
    with pytest.raises(ValidationError) as caught:
        check_expression_types("c = 5", kind="filter", columns=columns)
    message = str(caught.value)
    assert f"the type of `c` is {arrow_type}, which a policy cannot compare" in message
    assert "Unsigned integers" in message
    assert "on a table written by Lakana" in message
    check_expression_types("NULL", kind="mask", columns=columns, masked_column="c")
    with pytest.raises(ValidationError, match="which a mask cannot be checked against"):
        check_expression_types("0", kind="mask", columns=columns, masked_column="c")


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
def test_sql_spellings_of_a_type_are_understood_too(declared: str, family: str) -> None:
    """Iceberg and SQL type names type-check as before."""
    columns = [ColumnSpec(name="c", type=declared, nullable=True)]
    comparable = {
        "integer": "c = 1",
        "fractional": "c > 1.5",
        "text": "c = 'x'",
        "timestamp": "c > TIMESTAMP '2026-01-01 00:00:00'",
        "timestamptz": "c = c",
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


def test_a_clash_of_column_names_in_letter_case_is_refused_and_a_mask_is_typed_exactly() -> None:
    """Two columns that differ only in case: a read is ambiguous, a mask finds its own column."""
    columns = [
        ColumnSpec(name="Email", type="string", nullable=True),
        ColumnSpec(name="email", type="long", nullable=True),
    ]
    with pytest.raises(ValidationError, match="differ only in letter case"):
        check_expression_types("email = 'x'", kind="filter", columns=columns)
    with pytest.raises(ValidationError, match="`email` is long, but the mask is text"):
        check_expression_types("'***'", kind="mask", columns=columns, masked_column="email")
    check_expression_types("'***'", kind="mask", columns=columns, masked_column="Email")


def test_timestamptz_literals_are_refused_with_the_reason() -> None:
    """The message says why a time-zone-aware value is not accepted yet."""
    message = _refusal("seen = TIMESTAMPTZ '2026-01-01 00:00:00+00'")
    assert "UTC" not in message
    assert "not supported yet" in message


def test_a_decimal_without_parameters_has_duckdbs_default_precision_and_scale() -> None:
    """`decimal` alone is DECIMAL(18,3): three decimal places, fifteen digits before the point."""
    columns = [ColumnSpec(name="d", type="DECIMAL", nullable=True)]
    check_expression_types("d = 1.5", kind="filter", columns=columns)
    check_expression_types("d = 123456789012345.123", kind="filter", columns=columns)
    with pytest.raises(ValidationError, match="at most 3 decimal places and 15 digits"):
        check_expression_types("d = 1.5555", kind="filter", columns=columns)


def test_a_decimal_with_unreadable_parameters_is_an_unknown_type() -> None:
    """A type the check cannot read is refused when used, never guessed."""
    columns = [ColumnSpec(name="d", type="decimal(x,y)", nullable=True)]
    with pytest.raises(ValidationError, match="which a policy cannot compare"):
        check_expression_types("d = 1.5", kind="filter", columns=columns)


def test_a_number_literal_that_is_not_finite_has_no_exact_representation() -> None:
    """A literal with no finite value fits no column; the rule says so."""
    column = _column_type("long")
    for value in (None, Decimal("NaN"), Decimal("Infinity")):
        literal = _Type("fractional", "a decimal number", kind="literal", value=value)
        assert _rule_for(column, literal) == "a finite number"


def test_a_number_literal_that_cannot_be_read_is_refused() -> None:
    """A node no parser would make is refused with its text, not a crash."""
    checker = _Checker("x", "filter", COLUMNS)
    with pytest.raises(ValidationError, match="the number abc could not be read"):
        checker.type_of(exp.Literal.number("abc"))


def test_a_mask_that_is_a_column_of_an_unknown_type_names_that_column_and_its_type() -> None:
    """When only the mask's own type is unknown, the message names it, not the known column."""
    message = _refusal("tag", kind="mask", masked="n")
    assert "the type of `tag` is struct<a: int>, which a mask cannot be checked against" in message


@pytest.mark.parametrize("declared", ["DOUBLE", "Double", "FLOAT"])
def test_an_uppercase_float_spelling_is_a_float_not_a_decimal(declared: str) -> None:
    """The SQL spellings reach the float rules: any finite number in range, not three places."""
    columns = [ColumnSpec(name="x", type=declared, nullable=True)]
    check_expression_types("x > 1.2345", kind="filter", columns=columns)


def test_a_text_literal_for_a_number_column_is_suggested_without_leading_zeros() -> None:
    """`n = '007'` is told to compare with 7."""
    message = _refusal("id = '007'")
    assert "compare it with 7, not '007'." in message


_UNITS = ["s", "ms", "us"]


@pytest.mark.parametrize("unit", _UNITS)
@pytest.mark.parametrize(
    "expression",
    ["c > TIMESTAMP '2026-01-01 00:00:00'", "c > DATE '2026-01-01'", "c < TIMESTAMP '3000-01-01'"],
)
def test_a_parquet_timestamp_of_any_precision_up_to_micro_takes_a_timestamp_literal(
    unit: str, expression: str
) -> None:
    """DuckDB reads `timestamp[s]`, `[ms]` and `[us]` as TIMESTAMP: the comparison cannot fail."""
    columns = [ColumnSpec(name="c", type=_stored(pa.timestamp(unit)), nullable=True)]
    check_expression_types(expression, kind="filter", columns=columns)
    mask = "TIMESTAMP '1970-01-01 00:00:00'"
    assert check_expression_types(mask, kind="mask", columns=columns, masked_column="c")


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
def test_a_timestamp_with_a_zone_compares_only_with_a_zoned_column(unit: str) -> None:
    """The timestamptz rules hold at every precision: no date or timestamp literal."""
    columns = [ColumnSpec(name="c", type=_stored(pa.timestamp(unit, tz="UTC")), nullable=True)]
    check_expression_types("c = c", kind="filter", columns=columns)
    with pytest.raises(ValidationError, match="the read does not fix a time zone"):
        check_expression_types(
            "c > TIMESTAMP '2026-01-01 00:00:00'", kind="filter", columns=columns
        )


_NS = [ColumnSpec(name="c", type=_stored(pa.timestamp("ns")), nullable=True)]
_NS_RANGE = "from 1677-09-21 00:12:43.145225 to 2262-04-11 23:47:16.854775"


@pytest.mark.parametrize(
    "expression",
    [
        "c > TIMESTAMP '2026-01-01 00:00:00'",
        "c > DATE '2026-01-01'",
        "c < TIMESTAMP '2262-04-11 23:47:16.854775'",
        "c < DATE '2262-04-11'",
        "c > TIMESTAMP '1677-09-21 00:12:43.145225'",
        "c > DATE '1677-09-22'",
        "c = c",
    ],
)
def test_a_nanosecond_timestamp_takes_a_literal_inside_the_nanosecond_range(
    expression: str,
) -> None:
    """The literal is cast to TIMESTAMP_NS, which only fails outside the range."""
    check_expression_types(expression, kind="filter", columns=_NS)


@pytest.mark.parametrize(
    "expression",
    [
        "c < TIMESTAMP '2262-04-11 23:47:16.854776'",
        "c < DATE '2262-04-12'",
        "c > TIMESTAMP '3000-01-01 00:00:00'",
        "c > DATE '1677-09-21'",
        "c > TIMESTAMP '1500-01-01'",
        "TIMESTAMP '3000-01-01' > c",
    ],
)
def test_a_nanosecond_timestamp_refuses_a_literal_outside_the_nanosecond_range(
    expression: str,
) -> None:
    """The refusal names the range."""
    with pytest.raises(ValidationError) as caught:
        check_expression_types(expression, kind="filter", columns=_NS)
    assert f"`c` is timestamp[ns]; compare it with a date or timestamp {_NS_RANGE}" in str(
        caught.value
    )


def test_a_nanosecond_timestamp_is_not_compared_with_a_column_of_another_precision() -> None:
    """Casting a microsecond or date column to nanoseconds can overflow on a stored value."""
    columns = [
        *_NS,
        ColumnSpec(name="u", type=_stored(pa.timestamp("us")), nullable=True),
        ColumnSpec(name="d", type=_stored(pa.date32()), nullable=True),
    ]
    for expression in ("c > u", "d < c"):
        with pytest.raises(ValidationError, match="timestamp\\[ns\\] column"):
            check_expression_types(expression, kind="filter", columns=columns)


def test_a_mask_on_a_nanosecond_timestamp_is_accepted_only_as_null_or_a_timestamp() -> None:
    """No cast target exists, so the mask is reported unchecked; its kind must still be right."""
    columns = [
        *_NS,
        ColumnSpec(name="u", type=_stored(pa.timestamp("us")), nullable=True),
        ColumnSpec(name="m", type=_stored(pa.timestamp("ms")), nullable=True),
        ColumnSpec(name="d", type=_stored(pa.date32()), nullable=True),
    ]
    for mask in ("NULL", "c", "u", "m", "TIMESTAMP '1970-01-01 00:00:00'"):
        assert not check_expression_types(mask, kind="mask", columns=columns, masked_column="c")
    for mask, found in (
        ("'***'", "text"),
        ("d", "date32[day]"),
        ("0", "a number"),
        ("c = c", "boolean"),
    ):
        with pytest.raises(ValidationError) as caught:
            check_expression_types(mask, kind="mask", columns=columns, masked_column="c")
        assert (
            f"`c` is timestamp[ns], but the mask is {found}. Use a value of its own type:"
            in str(caught.value)
        )


def test_a_nanosecond_timestamp_is_not_compared_with_a_number_or_text() -> None:
    """Another kind is refused with the stored type named."""
    for expression, fragment in (
        ("c = 5", "`c` is timestamp[ns]; compare it with a timestamp such as TIMESTAMP"),
        ("c = 'x'", "`c` is timestamp[ns]; compare it with TIMESTAMP '2026-01-01 00:00:00'"),
    ):
        with pytest.raises(ValidationError) as caught:
            check_expression_types(expression, kind="filter", columns=_NS)
        assert fragment in str(caught.value)


def test_a_mask_on_a_nanosecond_timestamp_still_has_its_own_expression_checked() -> None:
    """A comparison or function inside the mask is type-checked as for any other mask."""
    columns = [*_NS, ColumnSpec(name="code", type="string", nullable=True)]
    with pytest.raises(ValidationError) as caught:
        check_expression_types("code = 5", kind="mask", columns=columns, masked_column="c")
    assert "`code` is text; compare it with '5', not 5." in str(caught.value)
    with pytest.raises(ValidationError, match="substr needs text"):
        check_expression_types("substr(c, 1, 2)", kind="mask", columns=columns, masked_column="c")
    with pytest.raises(ValidationError, match="but the mask is boolean"):
        check_expression_types("code = 'x'", kind="mask", columns=columns, masked_column="c")
