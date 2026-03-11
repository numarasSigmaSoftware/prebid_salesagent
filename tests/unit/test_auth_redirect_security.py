"""Tests for safe admin login redirect handling."""

from src.admin.auth_security import sanitize_next_url


def test_sanitize_next_url_allows_internal_relative_paths():
    assert sanitize_next_url("/admin/tenant/default") == "/admin/tenant/default"


def test_sanitize_next_url_blocks_absolute_external_urls():
    assert sanitize_next_url("https://evil.example.com/phish") is None


def test_sanitize_next_url_blocks_scheme_relative_urls():
    assert sanitize_next_url("//evil.example.com/phish") is None


def test_sanitize_next_url_blocks_encoded_absolute_urls():
    assert sanitize_next_url("%68%74%74%70%73%3A%2F%2Fevil.example.com") is None
