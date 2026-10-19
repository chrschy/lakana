"""Tests for the package index fixture: two indexes never share a URL."""

from __future__ import annotations

from index_fixture import Index, serving


def test_two_indexes_on_one_port_get_different_urls() -> None:
    """A reused ephemeral port must not make two tests register the same URL."""
    with serving() as first:
        one = first.registered.url()
        two = Index(first.registered.server).url()
        assert first.registered.port == Index(first.registered.server).port
        assert one != two


def test_an_index_keeps_its_url_and_serves_under_it() -> None:
    """The default URL is stable for one index and ends in `/simple`."""
    with serving() as indexes:
        assert indexes.registered.url() == indexes.registered.url()
        assert indexes.registered.url().endswith("/simple")
        assert indexes.registered.url() != indexes.other.url()


def test_an_explicit_prefix_is_used_as_given() -> None:
    """A test that names a prefix gets exactly that."""
    with serving() as indexes:
        assert (
            indexes.registered.url(prefix="/t2")
            == f"http://127.0.0.1:{indexes.registered.port}/t2/simple"
        )
