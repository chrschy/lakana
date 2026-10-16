"""Tests for the shared token-masking helper."""

import pytest

from lakana._tokens import mask_token


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("lakana_pat_abc_supersecret", "lakana_pat_abc_***"),
        ("lakana_task_xyz_supersecret", "lakana_task_xyz_***"),
        ("lakana_pat_abc123_x_y-z_secret", "lakana_pat_abc123_***"),
    ],
)
def test_mask_token_shows_only_prefix_and_key_id(token: str, expected: str) -> None:
    """Recognised token shapes mask everything after the key id."""
    assert mask_token(token) == expected


def test_mask_token_never_leaks_a_secret_with_underscores_or_dashes() -> None:
    """A secret containing underscores and dashes is fully hidden."""
    masked = mask_token("lakana_pat_abc123_x_y-z_secret")
    assert "x_y-z_secret" not in masked
    assert "secret" not in masked


def test_mask_token_hides_an_unrecognised_shape_entirely() -> None:
    """A token with no recognised prefix is fully masked."""
    assert mask_token("not-a-lakana-token") == "***"


def test_mask_token_hides_a_token_missing_its_secret_segment() -> None:
    """A token that looks like a prefix + key id but has no trailing secret is fully masked.

    The pattern requires a trailing underscore after the key id (the start of
    the secret segment); without it, the shape is not confidently
    recognised, so the whole thing is masked rather than partially shown.
    """
    assert mask_token("lakana_pat_abc123") == "***"
