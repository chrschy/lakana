"""Tests for raw-zone sample-file schema inference."""

import pytest

from lakana_core.catalog.schema_inference import infer_schema
from lakana_core.errors import ValidationError


def test_infer_schema_reads_a_csv_sample() -> None:
    """The CSV branch infers column names and types from a sample file's header and rows."""
    sample = b"id,name\n1,a\n2,b\n"

    schema = infer_schema(sample, "csv")

    assert schema == {"id": "int64", "name": "string"}


def test_infer_schema_reads_a_json_sample() -> None:
    """The JSON branch infers column names and types from newline-delimited sample records."""
    sample = b'{"id": 1, "name": "a"}\n{"id": 2, "name": "b"}\n'

    schema = infer_schema(sample, "json")

    assert schema == {"id": "int64", "name": "string"}


def test_infer_schema_rejects_an_unsupported_format() -> None:
    """A format outside the three supported ones is a validation error, not a crash."""
    with pytest.raises(ValidationError, match="xml"):
        infer_schema(b"<root/>", "xml")
