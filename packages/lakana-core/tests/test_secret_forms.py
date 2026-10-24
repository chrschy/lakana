"""`secret_forms`: every form a credential can take in text a task writes."""

from __future__ import annotations

from lakana_core.secrets.forms import MIN_SECRET_CHARS, secret_forms

_TRICKY = "Tr0ub\\4dor\"&'x\x01é/ +"


def test_every_form_a_print_a_repr_json_a_url_or_bytes_produce_is_listed() -> None:
    forms = secret_forms(_TRICKY)
    assert _TRICKY in forms
    assert "Tr0ub\\\\4dor\"&\\'x\\x01é/ +" in forms  # repr
    assert "Tr0ub\\\\4dor\\\"&'x\\u0001\\u00e9/ +" in forms  # JSON, ensure_ascii
    assert "Tr0ub\\\\4dor\\\"&'x\\u0001é/ +" in forms  # JSON, ensure_ascii=False
    assert "Tr0ub%5C4dor%22%26%27x%01%C3%A9/%20%2B" in forms  # quote
    assert "Tr0ub%5C4dor%22%26%27x%01%C3%A9%2F%20%2B" in forms  # quote, safe=""
    assert "Tr0ub%5C4dor%22%26%27x%01%C3%A9%2F+%2B" in forms  # quote_plus
    assert "Tr0ub\\\\4dor\"&\\'x\\x01\\xc3\\xa9/ +" in forms  # bytes repr
    assert len(forms) == 8


def test_a_plain_value_has_one_form() -> None:
    assert secret_forms("crm-db-pa55word") == {"crm-db-pa55word"}


def test_forms_shorter_than_the_minimum_are_left_out() -> None:
    assert MIN_SECRET_CHARS == 4
    assert secret_forms("abc") == set()
    assert secret_forms("a\nb") == {"a\\nb", "a%0Ab"}
