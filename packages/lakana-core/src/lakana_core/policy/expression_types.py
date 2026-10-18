"""Checking the types of a row filter or column mask expression against the table's columns.

`validate_restricted_expression` decides which *shapes* an expression may have. This module
decides whether it can run on the table's stored values without converting one type into
another, because a conversion that fails on a single value fails every query the policy covers.

The rules, all decided from the table's current schema when the policy is created:

* A comparison (`=`, `<>`, `<`, `<=`, `>`, `>=`) needs both sides of one kind: numbers with
  numbers (`int`, `long`, `float`, `double` and `decimal` among themselves, which never fails),
  text with text, a date or timestamp with a date or timestamp, and `boolean`, `time`, `uuid` and
  `binary` only with their own kind. `NULL` goes with anything.
* `AND`, `OR`, `NOT` and the filter as a whole need conditions: a boolean.
* `-` needs a number; `substr`/`substring`, `||` and `md5` need text (and, for `substr`, numbers).
* A column mask must produce its column's own type.
* Anything whose type cannot be decided is refused.

The types of the expression's parts (the function table):

* boolean: `TRUE`, `FALSE`, a comparison, `AND`, `OR`, `NOT`, `has_group(...)`;
* integer: a number without a decimal point or exponent, and `-` of one;
* decimal number: any other number;
* text: `'text'`, `current_user()`, `substr(...)`, `substring(...)`, `md5(...)` and the
  concatenation operator;
* date: `DATE 'YYYY-MM-DD'`; timestamp: `TIMESTAMP 'YYYY-MM-DD HH:MM:SS'`;
* `NULL`: fits any type;
* a column: its declared type.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import sqlglot.expressions as exp

from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.errors import ValidationError
from lakana_core.identifiers import shown
from lakana_core.policy.expressions import HAS_GROUP, validate_restricted_expression

_COMPARISONS: tuple[type[exp.Expression], ...] = (exp.EQ, exp.NEQ, exp.GT, exp.LT, exp.GTE, exp.LTE)
"""The comparison operators of the subset."""

_FAMILY_BY_NAME: dict[str, str] = {
    "boolean": "boolean",
    "bool": "boolean",
    "int": "integer",
    "integer": "integer",
    "long": "integer",
    "bigint": "integer",
    "smallint": "integer",
    "tinyint": "integer",
    "float": "fractional",
    "real": "fractional",
    "double": "fractional",
    "decimal": "fractional",
    "numeric": "fractional",
    "string": "text",
    "varchar": "text",
    "text": "text",
    "char": "text",
    "date": "date",
    "time": "time",
    "timestamp": "timestamp",
    "timestamptz": "timestamptz",
    "uuid": "uuid",
    "binary": "binary",
}
"""Declared type names (Iceberg's, and the usual SQL spellings of an external table) by family."""

_NUMBERS = frozenset({"integer", "fractional"})
_TEMPORAL = frozenset({"date", "timestamp", "timestamptz"})


@dataclass(frozen=True)
class _Type:
    """The type of an expression or a column.

    Parameters
    ----------
    family
        `"boolean"`, `"integer"`, `"fractional"`, `"text"`, `"date"`, `"timestamp"`,
        `"timestamptz"`, `"time"`, `"uuid"`, `"binary"`, `"null"` (the type of `NULL`, which
        fits anywhere) or `"unknown"` (a declared type this check has no rule for).
    name
        How a message names the type: a column's declared type, or a word for a value's.
    """

    family: str
    name: str


_NULL = _Type("null", "NULL")
_BOOLEAN = _Type("boolean", "boolean")
_TEXT = _Type("text", "text")


def _column_type(declared: str) -> _Type:
    """Read a column's declared type name.

    Parameters
    ----------
    declared
        The type as the table's schema spells it: `long`, `decimal(10,2)`, `string`, `VARCHAR`.

    Returns
    -------
    _Type
        Its family, named as declared (`text` for any text type), or the unknown family.
    """
    lowered = declared.strip().lower()
    family = _FAMILY_BY_NAME.get(lowered.split("(", 1)[0].strip())
    if family is None:
        return _Type("unknown", declared)
    return _Type(family, "text" if family == "text" else lowered)


def _group(family: str) -> str:
    """Name the kind of value a family is compared as.

    Parameters
    ----------
    family
        A type family.

    Returns
    -------
    str
        `"number"` for the numeric families, `"temporal"` for date and timestamps, otherwise the
        family itself.
    """
    if family in _NUMBERS:
        return "number"
    if family in _TEMPORAL:
        return "temporal"
    return family


_EXAMPLE_VALUE = {
    "text": "text such as 'x'",
    "integer": "a number",
    "fractional": "a number",
    "boolean": "TRUE or FALSE",
    "date": "a date such as DATE '2026-01-01'",
    "timestamp": "a timestamp such as TIMESTAMP '2026-01-01 00:00:00'",
    "timestamptz": "a timestamp such as TIMESTAMP '2026-01-01 00:00:00'",
}
"""What to compare a column with, by family, for the message."""

_MASK_VALUE = {
    "text": "'***', NULL, or a text function such as substr({column}, 1, 2)",
    "integer": "0 or NULL",
    "fractional": "0 or NULL",
    "boolean": "FALSE or NULL",
    "date": "DATE '1970-01-01' or NULL",
    "timestamp": "TIMESTAMP '1970-01-01 00:00:00' or NULL",
}
"""A value that keeps a masked column's type, by family, for the message."""


class _Checker:
    """Work out the type of every part of one expression and refuse what would need a cast.

    Parameters
    ----------
    sql_expr
        The expression as written, for messages.
    kind
        `"filter"` or `"mask"`, which words the messages.
    columns
        The table's columns.
    """

    def __init__(
        self, sql_expr: str, kind: Literal["filter", "mask"], columns: Sequence[ColumnSpec]
    ) -> None:
        self._sql_expr = sql_expr
        self._kind = kind
        self._columns = {spec.name.lower(): spec for spec in reversed(columns)}

    def refuse(self, detail: str) -> ValidationError:
        """Build the refusal of the expression.

        Parameters
        ----------
        detail
            What is wrong and what to write instead.

        Returns
        -------
        ValidationError
            The error to raise, naming the expression.
        """
        return ValidationError(
            f"'{shown(self._sql_expr)}' is not a valid {self._kind} expression: {detail}"
        )

    def describe(self, node: exp.Expression) -> str:
        """Name a part of the expression for a message.

        Parameters
        ----------
        node
            The part.

        Returns
        -------
        str
            The column's name in backquotes, `the number 5` or `the text 'x'` for a literal,
            otherwise the part as SQL, escaped for a message.
        """
        inner = _unwrap(node)
        if isinstance(inner, exp.Column):
            return f"`{shown(inner.name)}`"
        if isinstance(inner, exp.Literal):
            kind = "text" if inner.is_string else "number"
            return f"the {kind} {shown(inner.sql(dialect='duckdb'))}"
        if isinstance(inner, exp.Neg) and isinstance(inner.this, exp.Literal):
            return f"the number {shown(inner.sql(dialect='duckdb'))}"
        if isinstance(inner, exp.CurrentUser):
            return "current_user()"
        return shown(inner.sql(dialect="duckdb"))

    def type_of(self, node: exp.Expression) -> _Type:
        """Work out the type of an expression, refusing the first part that would need a cast.

        Parameters
        ----------
        node
            A node of the validated expression.

        Returns
        -------
        _Type
            Its type.

        Raises
        ------
        ValidationError
            If a part of it compares, combines or uses values of types that do not go together,
            or has a type that cannot be decided.
        """
        if isinstance(node, exp.Paren):
            return self.type_of(node.this)
        if isinstance(node, exp.Null):
            return _NULL
        if isinstance(node, exp.Boolean):
            return _BOOLEAN
        if isinstance(node, exp.Literal):
            if node.is_string:
                return _TEXT
            if node.name.isdecimal() and node.name.isascii():
                return _Type("integer", "a number")
            return _Type("fractional", "a decimal number")
        if isinstance(node, exp.Cast):
            return _typed_literal_type(node)
        if isinstance(node, exp.CurrentUser):
            return _TEXT
        if isinstance(node, exp.Anonymous) and node.name.lower() == HAS_GROUP:
            return _BOOLEAN
        if isinstance(node, exp.Column):
            spec = self._columns.get(node.name.lower())
            if spec is None:
                raise self.refuse(
                    f"the type of `{shown(node.name)}` is not known: the table has no such column."
                )
            return _column_type(spec.type)
        if isinstance(node, exp.Neg):
            inner = self.type_of(node.this)
            if inner.family not in _NUMBERS and inner.family != "null":
                self.require_known(node.this, inner)
                raise self.refuse(
                    f"{self.describe(node.this)} is {inner.name}, not a number; "
                    "a minus sign needs a number."
                )
            return inner
        if isinstance(node, (exp.And, exp.Or)):
            self.require_condition(node.this)
            self.require_condition(node.expression)
            return _BOOLEAN
        if isinstance(node, exp.Not):
            self.require_condition(node.this)
            return _BOOLEAN
        if isinstance(node, _COMPARISONS):
            self.check_comparison(node)
            return _BOOLEAN
        if isinstance(node, exp.DPipe):
            self.check_concatenation(node)
            return _TEXT
        if isinstance(node, exp.Substring):
            self.require_family(node.this, "text", "substr")
            for argument in (node.args.get("start"), node.args.get("length")):
                if argument is not None:
                    self.require_family(argument, "number", "substr")
            return _TEXT
        if isinstance(node, exp.MD5):
            self.require_family(node.this, "text", "md5")
            return _TEXT
        raise self.refuse(
            f"the type of {self.describe(node)} could not be determined, so it cannot be checked."
        )

    def require_known(self, node: exp.Expression, found: _Type) -> None:
        """Refuse an expression whose type this check has no rule for.

        Parameters
        ----------
        node
            The expression.
        found
            Its type.

        Raises
        ------
        ValidationError
            If the type is unknown.
        """
        if found.family == "unknown":
            raise self.refuse(
                f"the type of {self.describe(node)} is {shown(found.name)}, which a policy "
                "cannot compare. Use only columns of a simple type (text, number, boolean, "
                "date or timestamp)."
            )

    def require_condition(self, node: exp.Expression) -> None:
        """Refuse a part that is used as a condition but is not a boolean.

        Parameters
        ----------
        node
            The part.

        Raises
        ------
        ValidationError
            If its type is not boolean (or `NULL`).
        """
        found = self.type_of(node)
        self.require_known(node, found)
        if found.family in ("boolean", "null"):
            return
        inner = _unwrap(node)
        if isinstance(inner, exp.Column):
            example = _EXAMPLE_VALUE.get(found.family, "a value of its own type")
            raise self.refuse(
                f"{self.describe(node)} is {found.name}, not boolean; a condition is needed "
                f"here. Compare it with {example}, for example `{shown(inner.name)} = ...`."
            )
        if isinstance(inner, (exp.Literal, exp.Neg)):
            raise self.refuse(
                f"{self.describe(node)} is not a condition. Write a comparison such as "
                "`column = value`."
            )
        raise self.refuse(
            f"{self.describe(node)} is {found.name}, not boolean; a condition is needed here."
        )

    def require_family(self, node: exp.Expression, wanted: str, function: str) -> None:
        """Refuse an argument of a function that is not of the type the function takes.

        Parameters
        ----------
        node
            The argument.
        wanted
            `"text"` or `"number"`.
        function
            The function's name, for the message.

        Raises
        ------
        ValidationError
            If the argument has another type than `wanted` (`NULL` fits).
        """
        found = self.type_of(node)
        self.require_known(node, found)
        if found.family == "null" or _group(found.family) == wanted:
            return
        article = "text" if wanted == "text" else "a number"
        raise self.refuse(
            f"{self.describe(node)} is {found.name}, not {article}; {function} needs {article}."
        )

    def check_concatenation(self, node: exp.DPipe) -> None:
        """Refuse `||` of anything but text.

        Parameters
        ----------
        node
            The concatenation.

        Raises
        ------
        ValidationError
            If either side is not text.
        """
        sides = [(side, self.type_of(side)) for side in (node.this, node.expression)]
        for side, found in sides:
            self.require_known(side, found)
        if all(found.family in ("text", "null") for _, found in sides):
            return
        (left, left_type), (right, right_type) = sides
        raise self.refuse(
            f"{self.describe(left)} is {left_type.name} but {self.describe(right)} is "
            f"{right_type.name}; `||` joins text, so both sides must be text."
        )

    def check_comparison(self, node: exp.Expression) -> None:
        """Refuse a comparison whose sides are not of one kind.

        Parameters
        ----------
        node
            A comparison of the subset.

        Raises
        ------
        ValidationError
            If the sides would need an implicit conversion, naming the column, both types and what
            to write instead.
        """
        left, right = node.this, node.expression
        left_type, right_type = self.type_of(left), self.type_of(right)
        self.require_known(left, left_type)
        self.require_known(right, right_type)
        if "null" in (left_type.family, right_type.family):
            return
        if _group(left_type.family) == _group(right_type.family):
            return
        subject, subject_type, other, other_type = left, left_type, right, right_type
        if isinstance(_unwrap(right), exp.Column) and not isinstance(_unwrap(left), exp.Column):
            subject, subject_type, other, other_type = right, right_type, left, left_type
        raise self.refuse(self._mismatch(subject, subject_type, other, other_type))

    def _mismatch(
        self, subject: exp.Expression, subject_type: _Type, other: exp.Expression, other_type: _Type
    ) -> str:
        """Word the refusal of a comparison across types.

        Parameters
        ----------
        subject
            The column (or the left part) that sets the type.
        subject_type
            Its type.
        other
            The other side.
        other_type
            Its type.

        Returns
        -------
        str
            The sentence: both types and what to write instead.
        """
        head = f"{self.describe(subject)} is {subject_type.name}"
        inner = _unwrap(other)
        other_literal = isinstance(inner, exp.Literal) or (
            isinstance(inner, exp.Neg) and isinstance(inner.this, exp.Literal)
        )
        if not other_literal and not isinstance(inner, exp.Boolean):
            both_columns = isinstance(_unwrap(subject), exp.Column) and isinstance(
                inner, exp.Column
            )
            what = "columns" if both_columns else "values"
            return (
                f"{head} but {self.describe(other)} is {other_type.name}; "
                f"compare {what} of the same type."
            )
        shown_other = shown(inner.sql(dialect="duckdb"))
        family = subject_type.family
        if family in ("uuid", "time", "binary"):
            return (
                f"{head}; it can only be compared with another {subject_type.name} column "
                f"or NULL, not {shown_other}."
            )
        if family == "text" and other_type.family in _NUMBERS:
            return f"{head}; compare it with '{shown_other}', not {shown_other}."
        if family in _NUMBERS and other_type.family == "text" and isinstance(inner, exp.Literal):
            if inner.name.isascii() and inner.name.isdecimal():
                return f"{head}; compare it with {inner.name}, not {shown_other}."
            return f"{head}; compare it with a number, not {shown_other}."
        if family in _TEMPORAL and other_type.family == "text" and isinstance(inner, exp.Literal):
            typed = "DATE" if family == "date" else "TIMESTAMP"
            example = "2026-01-01" if family == "date" else "2026-01-01 00:00:00"
            return f"{head}; compare it with {typed} '{example}', not {shown_other}."
        example = _EXAMPLE_VALUE.get(family, "a value of its own type")
        return f"{head}; compare it with {example}, not {shown_other}."

    def check_mask(self, node: exp.Expression, column: str | None) -> None:
        """Refuse a column mask whose value is not of the masked column's type.

        Parameters
        ----------
        node
            The mask expression.
        column
            The masked column's name.

        Raises
        ------
        ValidationError
            If the mask's type differs from the column's, or either cannot be decided.
        """
        found = self.type_of(node)
        spec = self._columns.get((column or "").lower())
        if spec is None:
            raise self.refuse(
                f"the type of the masked column `{shown(column or '')}` is not known, "
                "so the mask cannot be checked."
            )
        wanted = _column_type(spec.type)
        name = f"`{shown(spec.name)}`"
        if found.family == "null":
            return
        if wanted.family == "unknown" or found.family == "unknown":
            if wanted == found:
                return
            raise self.refuse(
                f"the type of {name} is {shown(wanted.name)}, which a mask cannot be checked "
                "against. Use NULL."
            )
        if found.family == wanted.family or (
            wanted.family == "fractional" and found.family == "integer"
        ):
            return
        suggestion = _MASK_VALUE.get(wanted.family, "NULL").format(column=shown(spec.name))
        raise self.refuse(
            f"{name} is {wanted.name}, but the mask is {found.name}. "
            f"Use a value of its own type: {suggestion}."
        )


def _unwrap(node: exp.Expression) -> exp.Expression:
    """Strip the parentheses around a part.

    Parameters
    ----------
    node
        The part.

    Returns
    -------
    sqlglot.expressions.Expression
        What is inside the parentheses, or `node` itself.
    """
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _typed_literal_type(node: exp.Cast) -> _Type:
    """Give the type of a typed date or timestamp literal the subset accepted.

    Parameters
    ----------
    node
        A `DATE '...'` or `TIMESTAMP '...'` literal, already validated.

    Returns
    -------
    _Type
        `date` or `timestamp`.
    """
    if node.to.this is exp.DType.DATE:
        return _Type("date", "date")
    return _Type("timestamp", "timestamp")


def check_expression_types(
    sql_expr: str,
    *,
    kind: Literal["filter", "mask"],
    columns: Sequence[ColumnSpec],
    masked_column: str | None = None,
) -> None:
    """Refuse a row filter or column mask expression that would need an implicit conversion.

    Parameters
    ----------
    sql_expr
        The expression, as written.
    kind
        `"filter"` for a row-filter condition, `"mask"` for a column mask's replacement.
    columns
        The table's current columns with their declared types.
    masked_column
        For a mask, the column it replaces; its type is the type the mask must have.

    Raises
    ------
    ValidationError
        If a comparison puts two types against each other, a non-boolean is used as a
        condition, a function gets the wrong type, a mask has another type than its column, or
        a type cannot be decided. The message names the column, both types and what to write.
    """
    parsed = validate_restricted_expression(sql_expr, kind=kind)
    checker = _Checker(sql_expr, kind, columns)
    if kind == "mask":
        checker.check_mask(parsed, masked_column)
    else:
        checker.require_condition(parsed)
