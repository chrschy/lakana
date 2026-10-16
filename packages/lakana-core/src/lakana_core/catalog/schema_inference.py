"""Schema inference for raw-zone (external) tables.

Reading a sample file's schema is metadata inspection, not query execution —
it never runs untrusted SQL — so it runs synchronously here in lakana-core via
pyarrow, the same dependency `lakana_core.storage.resolver` already requires
for pyiceberg's `PyArrowFileIO`. Schema inference reads the sample file's
bytes through `Storage.read` first (verified to work against local and
S3 storage alike) and infers from an in-memory buffer,
rather than handing pyarrow a path or URL directly — pyarrow's own
filesystem resolution understands `s3://`/`file://` URIs with its own
credential handling, but not the presigned HTTPS URLs `resolve_read_url`
returns, and reusing `Storage.read` avoids needing pyarrow to have any S3
credential of its own at all.
"""

from __future__ import annotations

import io

import pyarrow.csv
import pyarrow.json
import pyarrow.parquet

from lakana_core.errors import ValidationError


def infer_schema(sample_bytes: bytes, file_format: str) -> dict[str, str]:
    """Infer a schema from one sample file's bytes.

    Parameters
    ----------
    sample_bytes
        The contents of one file matching the table's location pattern,
        already read through `Storage.read` (verified interactively against
        both local and S3 storage).
    file_format
        `"parquet"`, `"csv"`, or `"json"`.

    Returns
    -------
    dict
        Maps each inferred column name to its Arrow type's string form.

    Raises
    ------
    ValidationError
        If `file_format` is not one of the three supported formats.
    """
    if file_format == "parquet":
        schema = pyarrow.parquet.read_schema(io.BytesIO(sample_bytes))
    elif file_format == "csv":
        schema = pyarrow.csv.open_csv(io.BytesIO(sample_bytes)).schema
    elif file_format == "json":
        schema = pyarrow.json.read_json(io.BytesIO(sample_bytes)).schema
    else:
        raise ValidationError(f"Unsupported raw-zone file format: {file_format!r}.")
    return {field.name: str(field.type) for field in schema}
