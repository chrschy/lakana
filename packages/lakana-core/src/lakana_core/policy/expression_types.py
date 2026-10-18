"""Checking the types of a row filter or column mask expression against the table's columns.

`validate_restricted_expression` decides which *shapes* an expression may have. This module
decides whether it can run on the table's stored values without converting one type into
another, because a conversion that fails on a single value fails every query the policy covers.

The rules, all decided from the table's current schema when the policy is created. A comparison
is accepted only when DuckDB's implicit cast cannot fail:

* A comparison (`=`, `<>`, `<`, `<=`, `>`, `>=`) needs both sides of one kind. Text with text;
  `boolean`, `time`, `uuid` and `binary` only with their own kind; a `date` or `timestamp` with a
  `date`, a `timestamp` or a typed literal; a `timestamptz` only with a `timestamptz` column,
  because the read does not fix a time zone and any literal would mean another moment on another
  host. Comparing with `NULL` is refused (it never matches a row).
* Numbers, column against literal: the literal must be exactly representable in the column's
  type. An integer column takes a whole number within the type's range; `decimal(p,s)` takes at
  most `s` decimal places and `p - s` digits before the point, written without an exponent;
  `float` and `double` take a finite number within the type's range.
* Numbers, column against column: integer columns among themselves, `float`/`double` columns among
  themselves, and two `decimal` columns only when they have the same precision and scale. Any other
  pair (an integer with a `decimal` or a `double`, two different decimals) is refused.
* `AND`, `OR`, `NOT` and the filter as a whole need conditions: a boolean.
* A minus sign goes only in front of a number literal (`-5`): `-id` can overflow.
* `substr`/`substring` take text and whole-number literals in the 32-bit range as positions;
  `||` and `md5` take text.
* A column mask must keep its column's type: the mask is cast to the column's declared type at
  read time, and is accepted only when that cast cannot fail (the same representability rules).
* Anything whose type cannot be decided is refused.

The types of the expression's parts (the function table):

* boolean: `TRUE`, `FALSE`, a comparison, `AND`, `OR`, `NOT`, `has_group(...)`;
* integer: a number without a decimal point or exponent, and `-` of one;
* decimal number: any other number;
* text: `'text'`, `current_user()`, `substr(...)`, `substring(...)`, `md5(...)` and the
  concatenation operator;
* date: `DATE 'YYYY-MM-DD'`; timestamp: `TIMESTAMP 'YYYY-MM-DD HH:MM:SS'`;
* `NULL`: fits any type (as a mask);
* a column: its declared type.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Literal

import sqlglot.expressions as exp

from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.errors import ValidationError
from lakana_core.identifiers import shown
from lakana_core.policy.cast_types import arrow_type
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

_INTEGER_BITS = {"tinyint": 8, "smallint": 16, "int": 32, "integer": 32, "long": 64, "bigint": 64}
"""The width of each integer type name."""

_NUMBERS = frozenset({"integer", "fractional"})
_TEMPORAL = frozenset({"date", "timestamp"})

_DEFAULT_DECIMAL = (18, 3)
"""DuckDB's `DECIMAL` without parameters."""

_FLOAT_MAX = {
    "float": Decimal("3.4028234663852886e38"),
    "double": Decimal("1.7976931348623157e308"),
}
_FLOAT_MAX_SHOWN = {"float": "3.4028235e+38", "double": "1.7976931348623157e+308"}
"""The largest finite value of each float type, and how a message writes it."""

_INT_DIGITS = {8: 3, 16: 5, 32: 10, 64: 19}
"""The most digits a value of each integer width has."""

_POSITION_BITS = 32
"""`substr` takes positions of this width."""


@dataclass(frozen=True)
class _Type:
    """The type of an expression or a column.

    Parameters
    ----------
    family
        `"boolean"`, `"integer"`, `"fractional"`, `"text"`, `"date"`, `"timestamp"`,
        `"timestamptz"`, `"time"`, `"uuid"`, `"binary"`, `"null"` (the type of `NULL`) or
        `"unknown"` (a declared type this check has no rule for).
    name
        How a message names the type: a column's declared type, or a word for a value's.
    kind
        For a number: `"int"`, `"float"`, `"double"`, `"decimal"` for a column, `"literal"` for a
        number written in the expression. Empty otherwise.
    bits
        An integer column's width.
    precision, scale
        A decimal column's precision and scale.
    value
        A number literal's value.
    plain
        Whether a number literal is written without an exponent.
    """

    family: str
    name: str
    kind: str = ""
    bits: int | None = None
    precision: int | None = None
    scale: int | None = None
    value: Decimal | None = None
    plain: bool = True


_NULL = _Type("null", "NULL")
_BOOLEAN = _Type("boolean", "boolean")
_TEXT = _Type("text", "text")


_ARROW_FAMILIES: dict[exp.DType, tuple[str, str, int | None]] = {
    exp.DType.TINYINT: ("integer", "int", 8),
    exp.DType.SMALLINT: ("integer", "int", 16),
    exp.DType.INT: ("integer", "int", 32),
    exp.DType.BIGINT: ("integer", "int", 64),
    exp.DType.FLOAT: ("fractional", "float", None),
    exp.DType.DOUBLE: ("fractional", "double", None),
    exp.DType.BOOLEAN: ("boolean", "", None),
    exp.DType.VARCHAR: ("text", "", None),
    exp.DType.VARBINARY: ("binary", "", None),
    exp.DType.DATE: ("date", "", None),
    exp.DType.TIMESTAMP: ("timestamp", "", None),
    exp.DType.TIMESTAMPTZ: ("timestamptz", "", None),
}
"""The family, number kind and integer width of each DuckDB type a stored Arrow name maps to."""


def _arrow_column_type(declared: str, kind: exp.DType, params: tuple[int, ...]) -> _Type:
    """Type a column whose declared type is a stored Arrow name.

    Parameters
    ----------
    declared
        The stored name (`int64`, `date32[day]`, `decimal128(10, 2)`), kept for messages.
    kind
        The DuckDB type `lakana_core.policy.cast_types.arrow_type` read from it.
    params
        Its integer parameters: precision and scale of a decimal.

    Returns
    -------
    _Type
        The column's type; the name is the stored one, `text` for any text type.
    """
    if kind is exp.DType.DECIMAL:
        return _Type("fractional", declared, kind="decimal", precision=params[0], scale=params[1])
    family, number, bits = _ARROW_FAMILIES[kind]
    return _Type(family, "text" if family == "text" else declared, kind=number, bits=bits)


def _column_type(declared: str) -> _Type:
    """Read a column's declared type name.

    Parameters
    ----------
    declared
        The type as the table's schema spells it: `long`, `decimal(10,2)`, `string`, `VARCHAR`.

    Returns
    -------
    _Type
        Its family and, for a number, its width or precision and scale; named as declared (`text`
        for any text type), or the unknown family.
    """
    stored = arrow_type(declared)
    if stored is not None:
        return _arrow_column_type(declared, *stored)
    lowered = declared.strip().lower()
    base, _, params = lowered.partition("(")
    base = base.strip()
    family = _FAMILY_BY_NAME.get(base)
    if family is None:
        return _Type("unknown", declared)
    if family == "text":
        return _Type("text", "text")
    if family == "integer":
        return _Type(family, lowered, kind="int", bits=_INTEGER_BITS[base])
    if family == "fractional":
        if base == "real":  # `float` and `double` are read through the Arrow names
            return _Type(family, lowered, kind="float")
        precision, scale = _DEFAULT_DECIMAL
        if params:
            try:
                first, second = params.rstrip(") ").split(",")
                precision, scale = int(first), int(second)
            except ValueError:
                return _Type("unknown", declared)
        return _Type(family, lowered, kind="decimal", precision=precision, scale=scale)
    return _Type(family, lowered)


def _group(family: str) -> str:
    """Name the kind of value a family is compared as.

    Parameters
    ----------
    family
        A type family.

    Returns
    -------
    str
        `"number"` for the numeric families, `"temporal"` for date and timestamp, otherwise the
        family itself (a `timestamptz` compares with nothing but itself).
    """
    if family in _NUMBERS:
        return "number"
    if family in _TEMPORAL:
        return "temporal"
    return family


def _digits(value: Decimal) -> tuple[int, int]:
    """Count the digits of a number before and after the decimal point.

    Parameters
    ----------
    value
        A finite number, as written (`1.50` has two decimal places).

    Returns
    -------
    tuple of int
        The digits before the point and the decimal places.
    """
    _, digits, special = value.as_tuple()
    exponent = int(special)  # finite by the caller's check, so an integer
    if value == 0:
        return 0, max(0, -exponent)
    return max(0, len(digits) + exponent), max(0, -exponent)


def _rule_for(target: _Type, literal: _Type) -> str | None:
    """Say what a number literal must look like to fit a column, if it does not.

    Parameters
    ----------
    target
        The column's type (a number).
    literal
        The literal's type, with its value.

    Returns
    -------
    str or None
        The rule the literal breaks, worded to follow "compare it with": `None` when the literal
        is exactly representable in the column's type, so DuckDB's cast of it cannot fail.
    """
    value = literal.value
    if value is None or not value.is_finite():
        return "a finite number"
    if target.kind == "int":
        bits = target.bits or 64
        low, high = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
        if literal.family != "integer" or not literal.plain:
            return "a whole number such as 5"
        if not low <= value <= high:
            return f"a value from {low} to {high}"
        return None
    if target.kind == "decimal":
        if not literal.plain:
            return "a number written without an exponent"
        before, after = _digits(value)
        precision, scale = target.precision or 0, target.scale or 0
        if after > scale or before > precision - scale:
            return (
                f"a number with at most {scale} decimal places and "
                f"{precision - scale} digits before the point"
            )
        return None
    limit = _FLOAT_MAX[target.kind]
    if abs(value) > limit:
        shown_limit = _FLOAT_MAX_SHOWN[target.kind]
        return f"a number from -{shown_limit} to {shown_limit}"
    return None


def _column_fits(target: _Type, found: _Type) -> bool:
    """Say whether every value of one number column fits another under a cast.

    Parameters
    ----------
    target
        The masked column's type.
    found
        The type of the column the mask reads.

    Returns
    -------
    bool
        `True` when casting any value of `found` to `target` cannot fail.
    """
    if found.family == "integer" and target.kind == "int":
        return (found.bits or 64) <= (target.bits or 64)
    if found.kind == "float" and target.kind in ("float", "double"):
        return True
    if found.kind == "double" and target.kind == "double":
        return True
    if target.kind == "decimal":
        before_target = (target.precision or 0) - (target.scale or 0)
        if found.kind == "decimal":
            before = (found.precision or 0) - (found.scale or 0)
            return (found.scale or 0) <= (target.scale or 0) and before <= before_target
        if found.family == "integer":
            return _INT_DIGITS[found.bits or 64] <= before_target
    return False


_EXAMPLE_VALUE = {
    "text": "text such as 'x'",
    "integer": "a number",
    "fractional": "a number",
    "boolean": "TRUE or FALSE",
    "date": "a date such as DATE '2026-01-01'",
    "timestamp": "a timestamp such as TIMESTAMP '2026-01-01 00:00:00'",
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
        self._exact = {spec.name: spec for spec in reversed(columns)}
        self._by_lower: dict[str, list[ColumnSpec]] = {}
        for spec in columns:
            self._by_lower.setdefault(spec.name.lower(), []).append(spec)

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
            return self._literal_type(node)
        if isinstance(node, exp.Cast):
            return _typed_literal_type(node)
        if isinstance(node, exp.CurrentUser):
            return _TEXT
        if isinstance(node, exp.Anonymous) and node.name.lower() == HAS_GROUP:
            return _BOOLEAN
        if isinstance(node, exp.Column):
            return self._column(node)
        if isinstance(node, exp.Neg):
            inner = _unwrap(node.this)
            if not (isinstance(inner, exp.Literal) and not inner.is_string):
                raise self.refuse(
                    f"{self.describe(node.this)} cannot follow a minus sign. A minus sign can "
                    "only be written in front of a number such as -5."
                )
            literal = self.type_of(inner)
            return replace(literal, value=-(literal.value or Decimal(0)))
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
                    self.require_position(argument, node.this)
            return _TEXT
        if isinstance(node, exp.MD5):
            self.require_family(node.this, "text", "md5")
            return _TEXT
        raise self.refuse(
            f"the type of {self.describe(node)} could not be determined, so it cannot be checked."
        )

    def _literal_type(self, node: exp.Literal) -> _Type:
        """Give the type of a text or number literal.

        Parameters
        ----------
        node
            The literal.

        Returns
        -------
        _Type
            Text; or an integer or decimal number with its value.

        Raises
        ------
        ValidationError
            If the number cannot be read.
        """
        if node.is_string:
            return _TEXT
        try:
            value = Decimal(node.name)
        except InvalidOperation as exc:
            raise self.refuse(f"the number {shown(node.name)} could not be read.") from exc
        plain = node.name.replace(".", "", 1).isdecimal() and node.name.isascii()
        if node.name.isdecimal() and node.name.isascii():
            return _Type("integer", "a number", kind="literal", value=value, plain=True)
        return _Type("fractional", "a decimal number", kind="literal", value=value, plain=plain)

    def _column(self, node: exp.Column) -> _Type:
        """Look up the type of a column the expression reads.

        Parameters
        ----------
        node
            The column reference.

        Returns
        -------
        _Type
            The column's declared type.

        Raises
        ------
        ValidationError
            If the table has no such column, or has several that differ only in letter case.
        """
        found = self._by_lower.get(node.name.lower(), [])
        if not found:
            raise self.refuse(
                f"the type of `{shown(node.name)}` is not known: the table has no such column."
            )
        if len(found) > 1:
            names = ", ".join(f"`{shown(spec.name)}`" for spec in found)
            raise self.refuse(
                f"the table has columns that differ only in letter case ({names}), so "
                f"`{shown(node.name)}` is ambiguous."
            )
        return _column_type(found[0].type)

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
                "date or timestamp). Unsigned integers, timestamps in seconds, milliseconds or "
                "nanoseconds, and nested types are not supported yet."
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
            `"text"`.
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
        raise self.refuse(
            f"{self.describe(node)} is {found.name}, not text; {function} needs text."
        )

    def require_position(self, node: exp.Expression, subject: exp.Expression) -> None:
        """Refuse a `substr` position or length that is not a whole-number literal.

        Parameters
        ----------
        node
            The position or length.
        subject
            The text `substr` cuts, for the example in the message.

        Raises
        ------
        ValidationError
            If it is not a whole number written in the expression within the 32-bit range.
        """
        inner = _unwrap(node)
        found = self.type_of(inner) if isinstance(inner, (exp.Literal, exp.Neg)) else None
        low, high = -(2 ** (_POSITION_BITS - 1)), 2 ** (_POSITION_BITS - 1) - 1
        if (
            found is not None
            and found.family == "integer"
            and found.plain
            and found.value is not None
            and low <= found.value <= high
        ):
            return
        raise self.refuse(
            f"{self.describe(node)} cannot be a position: substr needs a whole number such as "
            f"substr({self.describe(subject).strip('`')}, 1, 2), written as a number from "
            f"{low} to {high}."
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
        """Refuse a comparison whose sides are not of one kind, or whose cast could fail.

        Parameters
        ----------
        node
            A comparison of the subset.

        Raises
        ------
        ValidationError
            If the sides would need an implicit conversion that can fail, naming the column,
            both types and what to write instead.
        """
        left, right = node.this, node.expression
        left_type, right_type = self.type_of(left), self.type_of(right)
        self.require_known(left, left_type)
        self.require_known(right, right_type)
        if "null" in (left_type.family, right_type.family):
            raise self.refuse(
                "comparing with NULL never matches a row (the result is always NULL). "
                "`IS NULL` is not supported yet; compare the column with a value instead."
            )
        if "timestamptz" in (left_type.family, right_type.family):
            if left_type.family == right_type.family and all(
                isinstance(_unwrap(side), exp.Column) for side in (left, right)
            ):
                return
            self._refuse_timestamptz(left, left_type, right, right_type)
        if _group(left_type.family) != _group(right_type.family):
            subject, subject_type, other, other_type = left, left_type, right, right_type
            if isinstance(_unwrap(right), exp.Column) and not isinstance(_unwrap(left), exp.Column):
                subject, subject_type, other, other_type = right, right_type, left, left_type
            raise self.refuse(self._mismatch(subject, subject_type, other, other_type))
        if left_type.family in _NUMBERS:
            self._check_numbers(left, left_type, right, right_type)

    def _refuse_timestamptz(
        self, left: exp.Expression, left_type: _Type, right: exp.Expression, right_type: _Type
    ) -> None:
        """Refuse a comparison that involves a `timestamptz` and anything but another column.

        Parameters
        ----------
        left
            The left side.
        left_type
            Its type.
        right
            The right side.
        right_type
            Its type.

        Raises
        ------
        ValidationError
            Always: the read does not fix a time zone, so the result would depend on the host.
        """
        subject, other = (left, right) if left_type.family == "timestamptz" else (right, left)
        raise self.refuse(
            f"{self.describe(subject)} is timestamptz; the read does not fix a time zone, so "
            f"comparing it with {self.describe(other)} would select different rows on different "
            "hosts. Compare it only with another timestamptz column, or store the column as a "
            "timestamp. Time-zone-aware literals are not supported yet."
        )

    def _check_numbers(
        self, left: exp.Expression, left_type: _Type, right: exp.Expression, right_type: _Type
    ) -> None:
        """Refuse a numeric comparison in which DuckDB's cast can fail.

        Parameters
        ----------
        left
            The left side.
        left_type
            Its type.
        right
            The right side.
        right_type
            Its type.

        Raises
        ------
        ValidationError
            If a literal is not exactly representable in the column's type, or two columns are
            not of one kind (integers, floats, or decimals of one precision and scale).
        """
        left_column = isinstance(_unwrap(left), exp.Column)
        right_column = isinstance(_unwrap(right), exp.Column)
        if left_column and right_column:
            same_kind = (
                (left_type.family == "integer" and right_type.family == "integer")
                or (
                    left_type.kind in ("float", "double") and right_type.kind in ("float", "double")
                )
                or (
                    left_type.kind == "decimal"
                    and right_type.kind == "decimal"
                    and (left_type.precision, left_type.scale)
                    == (right_type.precision, right_type.scale)
                )
            )
            if not same_kind:
                raise self.refuse(
                    f"{self.describe(left)} is {left_type.name} but {self.describe(right)} is "
                    f"{right_type.name}; compare columns of the same kind (whole numbers with "
                    "whole numbers, float or double with float or double, decimals of the same "
                    "precision and scale)."
                )
            return
        if left_column or right_column:
            column, column_type, literal, literal_type = (
                (left, left_type, right, right_type)
                if left_column
                else (right, right_type, left, left_type)
            )
            rule = _rule_for(column_type, literal_type)
            if rule is not None:
                text = shown(_unwrap(literal).sql(dialect="duckdb"))
                raise self.refuse(
                    f"{self.describe(column)} is {column_type.name}; compare it with {rule}, "
                    f"not {text}."
                )

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
        """Refuse a column mask that does not keep the masked column's type.

        The mask is cast to the column's declared type at read time, so the value must be one
        the cast cannot fail on.

        Parameters
        ----------
        node
            The mask expression.
        column
            The masked column's name, matched exactly.

        Raises
        ------
        ValidationError
            If the mask's type differs from the column's, may not fit it, or either cannot be
            decided.
        """
        found = self.type_of(node)
        spec = self._exact.get(column or "")
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
            # The declared strings are compared exactly: Iceberg spells a type one way only, so
            # "the column itself" always matches itself; a differently spelled type is refused.
            if wanted == found:
                return
            blamed, blamed_type = (
                (name, wanted) if wanted.family == "unknown" else (self.describe(node), found)
            )
            raise self.refuse(
                f"the type of {blamed} is {shown(blamed_type.name)}, which a mask cannot be "
                "checked against. Use NULL."
            )
        suggestion = _MASK_VALUE.get(wanted.family, "NULL").format(column=shown(spec.name))
        same = found.family == wanted.family
        widening = wanted.family == "fractional" and found.family == "integer"
        if not (same or widening):
            raise self.refuse(
                f"{name} is {wanted.name}, but the mask is {found.name}. "
                f"Use a value of its own type: {suggestion}."
            )
        if wanted.family not in _NUMBERS:
            return
        inner = _unwrap(node)
        text = shown(inner.sql(dialect="duckdb"))
        if found.value is not None:
            rule = _rule_for(wanted, found)
            if rule is None:
                return
            raise self.refuse(
                f"{name} is {wanted.name}, but the mask {text} may not fit it (it needs {rule}). "
                f"Use a value of its own type: {suggestion}."
            )
        if isinstance(inner, exp.Column) and _column_fits(wanted, found):
            return
        raise self.refuse(
            f"{name} is {wanted.name}, but the mask {self.describe(node)} is {found.name} and "
            f"may not fit it. Use a value of its own type: {suggestion}."
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
    """Refuse a row filter or column mask expression that needs a conversion that can fail.

    Parameters
    ----------
    sql_expr
        The expression, as written.
    kind
        `"filter"` for a row-filter condition, `"mask"` for a column mask's replacement.
    columns
        The table's current columns with their declared types.
    masked_column
        For a mask, the column it replaces (matched exactly); its type is the type the mask must
        have.

    Raises
    ------
    ValidationError
        If a comparison puts two types against each other or could fail on a stored value, a
        non-boolean is used as a condition, a function gets the wrong type, a mask has another
        type than its column or may not fit it, or a type cannot be decided. The message names
        the column, both types and what to write.
    """
    parsed = validate_restricted_expression(sql_expr, kind=kind)
    checker = _Checker(sql_expr, kind, columns)
    if kind == "mask":
        checker.check_mask(parsed, masked_column)
    else:
        checker.require_condition(parsed)
