"""The SDK's copy of the artifact manifest's path rules and canonical form."""

from __future__ import annotations

import hashlib

import pytest

from lakana._artifact_manifest import (
    ManifestError,
    canonical_manifest,
    fold_path,
    manifest_digest,
    path_problem,
    paths_problem,
    shown,
)
from lakana.errors import ValidationError

_A = hashlib.sha256(b"a").hexdigest()
_B = hashlib.sha256(b"b").hexdigest()
_MODEL = "68ad4f90fd5eedf03d989cd614b1a1da92ccb7965662d6eaf2524c85dbb8325b"
_UNSAFE_TEXT = "must not contain a control, format or surrogate character or a line separator"
_LONG_PART = "must not have a part longer than 255 bytes"
_SEGMENT = "must not have an empty, '.' or '..' segment"


def test_the_canonical_form_is_fixed() -> None:
    files = [("model.json", 14, _MODEL)]
    expected = (
        b'{"files":[{"path":"model.json","sha256":"' + _MODEL.encode() + b'","size":14}],'
        b'"format":1,"is_directory":false}'
    )
    assert canonical_manifest(False, files) == expected
    assert manifest_digest(False, files) == hashlib.sha256(expected).hexdigest()


def test_the_order_of_the_input_does_not_change_the_digest() -> None:
    files = [("a/b.bin", 3, _A), ("model.json", 14, _MODEL), ("z", 1, _B)]
    assert manifest_digest(True, files[::-1]) == manifest_digest(True, files)


def test_the_canonical_form_is_ascii_whatever_the_path() -> None:
    assert b'"path":"\\u00e9/\\u4e2d.bin"' in canonical_manifest(False, [("é/\u4e2d.bin", 1, _A)])


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        ("../etc/passwd", _SEGMENT),
        ("a/../b", _SEGMENT),
        ("a/./b", _SEGMENT),
        ("./a", _SEGMENT),
        ("a//b", _SEGMENT),
        ("a/", _SEGMENT),
        ("/etc/passwd", "must be relative, without a leading '/'"),
        ("a\\b", "must not contain a backslash; separate segments with '/'"),
        ("C:/x", "must not contain ':'"),
        ("a\x00b", _UNSAFE_TEXT),
        ("a\nb", _UNSAFE_TEXT),
        ("a\u202eb", _UNSAFE_TEXT),
        ("a\u200db", _UNSAFE_TEXT),
        ("a\ud800b", _UNSAFE_TEXT),
        ("a\u2028b", _UNSAFE_TEXT),
        ("x" * 1025, "is longer than 1024 bytes"),
        ("é" * 513, "is longer than 1024 bytes"),
        ("/".join("a" * 33), "has more than 32 segments"),
        ("x" * 256, _LONG_PART),
        ("a/" + "é" * 128, _LONG_PART),
        ("\U0001f600" * 64, _LONG_PART),
        ("", "a path must be a non-empty string"),
        (3, "a path must be a non-empty string"),
        (None, "a path must be a non-empty string"),
    ],
)
def test_a_path_that_leaves_its_directory_is_refused(path: object, problem: str) -> None:
    assert path_problem(path) == problem


def test_the_longest_allowed_path_passes() -> None:
    assert path_problem("/".join(["x" * 255] * 4)) is None
    assert path_problem("x" * 255) is None
    assert path_problem("é" * 127 + "x") is None  # 255 bytes in 128 characters
    assert path_problem("\U0001f600" * 63 + "xxx") is None  # 255 bytes in 66 characters
    assert path_problem("/".join("a" * 32)) is None
    assert path_problem("weights/layer 1/Ünïcode.bin") is None


def test_a_shown_path_cannot_forge_a_line() -> None:
    assert shown("a\nb\x1b[31m") == "a\\nb\\x1b[31m"
    assert len(shown("x" * 5000)) == 200
    assert shown(3) == "int"


@pytest.mark.parametrize(
    ("paths", "message"),
    [
        (["Model.bin", "model.bin"], "Paths Model.bin and model.bin differ only by case"),
        (["cafe\u0301", "caf\u00e9"], "differ only by case or Unicode normalisation"),
        (["STRASSE", "stra\u00dfe"], "differ only by case or Unicode normalisation"),
        (["A/x", "a/y"], "Directories A and a differ only by case"),
        (["a", "a/b"], "a is both a file and a directory."),
        (["A", "a/b"], "A is both a file and a directory."),
    ],
)
def test_paths_that_collide_after_folding_are_refused(paths: list[str], message: str) -> None:
    ordered = sorted(paths, key=lambda p: p.encode("utf-8"))
    assert message in (paths_problem(ordered) or "")


def test_folding_is_the_one_discovery_uses() -> None:
    assert fold_path("Model.JSON") == fold_path("model.json")
    assert fold_path("a/B") != fold_path("a/c")
    assert paths_problem(["a/x", "a/y", "b"]) is None
    assert paths_problem(["a/b/c", "a/b/d", "a/e/f", "ab/c", "b"]) is None


def test_a_manifest_error_is_the_sdks_validation_error() -> None:
    error = ManifestError("Path x is bad.")
    assert isinstance(error, ValidationError)
    assert str(error) == error.detail == "Path x is bad."


def test_the_canonical_order_is_the_utf8_byte_order() -> None:
    """Pins the sort key against UTF-16 order, which puts U+10000 before U+FFFF."""
    low, high = "\uffff", "\U00010000"
    assert path_problem(low) is None
    assert path_problem(high) is None
    files = [(high, 1, _A), (low, 2, _B)]
    canonical = canonical_manifest(True, files)
    assert canonical.index(b"\\uffff") < canonical.index(b"\\ud800\\udc00")
    assert canonical == canonical_manifest(True, files[::-1])
    assert manifest_digest(True, files) == hashlib.sha256(canonical).hexdigest()


_COLON = "must not contain ':'"
_END = "must not have a part ending in '.' or a space"
_NAME = "must not use a name Windows reserves, such as 'NUL'"


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        ("a/C:x", _COLON),
        ("x/y:stream", _COLON),
        ("a./b", _END),
        ("a /b", _END),
        ("x/a.", _END),
        ("dir/NUL", _NAME),
        ("nul.txt", _NAME),
        ("Con", _NAME),
        ("com1.tar.gz", _NAME),
        ("COM\u00b9", _NAME),
        ("lpt\u00b2.txt", _NAME),
        ("NUL .txt", _NAME),
        ("conin$", _NAME),
        ("sub/CONOUT$.txt", _NAME),
        ("a:/..", _SEGMENT),
        ("a./..", _SEGMENT),
    ],
)
def test_windows_hazards_are_refused_with_their_own_text(path: str, problem: str) -> None:
    assert path_problem(path) == problem


@pytest.mark.parametrize(
    "path",
    [
        "console",
        "nul_1",
        "con1",
        "com10",
        "com",
        "COM\u2074",
        "NU\u017f",
        "nul\u0131",
        "a.nul",
        'a<b>c|d?e*f"g',
        "a b/c",
    ],
)
def test_near_misses_of_the_windows_rules_pass(path: str) -> None:
    assert path_problem(path) is None
