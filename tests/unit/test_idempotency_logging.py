"""Idempotency replay credentials never reach logs in full."""

from src.core.idempotency_logging import redact_idempotency_key


def test_redactor_exposes_only_eight_character_prefix() -> None:
    key = "abcdefgh-sensitive-replay-credential"

    assert redact_idempotency_key(key) == "abcdefgh…"
    assert key not in redact_idempotency_key(key)


def test_redactor_marks_missing_key_without_stringifying_it() -> None:
    assert redact_idempotency_key(None) == "<absent>"
