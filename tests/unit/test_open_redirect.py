"""Tests for F-06: open redirect via login 'next' parameter.

Verifies _safe_redirect() blocks absolute URLs, scheme-relative URLs,
and percent-encoded variants, while allowing safe relative paths.
"""

import pytest


class TestSafeRedirect:
    """_safe_redirect() must sanitise the next URL before use."""

    @pytest.fixture(autouse=True)
    def import_helper(self):
        from src.admin.blueprints.auth import _safe_redirect

        self._safe_redirect = _safe_redirect

    def test_accepts_relative_path(self):
        """Simple relative paths should pass through unchanged."""
        assert self._safe_redirect("/admin/tenant/default", fallback="/") == "/admin/tenant/default"

    def test_accepts_relative_path_with_query(self):
        result = self._safe_redirect("/admin/tenant/default?tab=products", fallback="/")
        assert result == "/admin/tenant/default?tab=products"

    def test_rejects_relative_path_without_leading_slash(self):
        """Bare relative paths should not be used as redirect targets."""
        result = self._safe_redirect("tenant/default", fallback="/safe")
        assert result == "/safe"

    def test_rejects_absolute_http_url(self):
        """Absolute http:// URLs must be replaced with fallback."""
        result = self._safe_redirect("http://evil.com/steal", fallback="/safe")
        assert result == "/safe"

    def test_rejects_absolute_https_url(self):
        """Absolute https:// URLs must be replaced with fallback."""
        result = self._safe_redirect("https://evil.com/", fallback="/safe")
        assert result == "/safe"

    def test_rejects_scheme_relative_url(self):
        """Scheme-relative //evil.com URLs must be replaced with fallback."""
        result = self._safe_redirect("//evil.com/path", fallback="/safe")
        assert result == "/safe"

    def test_rejects_percent_encoded_scheme_relative(self):
        """URL-encoded variant %2F%2Fevil.com must be rejected."""
        result = self._safe_redirect("%2F%2Fevil.com/path", fallback="/safe")
        assert result == "/safe"

    def test_rejects_percent_encoded_absolute(self):
        """URL-encoded http%3A//evil.com must be rejected."""
        result = self._safe_redirect("http%3A//evil.com", fallback="/safe")
        assert result == "/safe"

    def test_rejects_backslash_prefixed_host(self):
        """Backslash-prefixed host tricks must be rejected."""
        result = self._safe_redirect("\\\\evil.com\\payload", fallback="/safe")
        assert result == "/safe"

    def test_returns_fallback_for_none(self):
        """None input returns fallback unchanged."""
        assert self._safe_redirect(None, fallback="/default") == "/default"

    def test_returns_fallback_for_empty_string(self):
        """Empty string returns fallback."""
        assert self._safe_redirect("", fallback="/default") == "/default"

    def test_rejects_javascript_scheme(self):
        """javascript: protocol must be rejected."""
        result = self._safe_redirect("javascript:alert(1)", fallback="/safe")
        assert result == "/safe"

    def test_rejects_ftp_scheme(self):
        """ftp:// URLs must be rejected."""
        result = self._safe_redirect("ftp://files.evil.com/payload", fallback="/safe")
        assert result == "/safe"


class TestNextUrlIngestionInLoginRoute:
    """F-06: login route stores next_url only after validation."""

    def test_safe_redirect_called_at_ingestion(self):
        """login() must validate 'next' before storing in session."""
        from pathlib import Path

        source = Path("src/admin/blueprints/auth.py").read_text()
        # The fix wraps the ingestion in _safe_redirect:
        assert '_safe_redirect(request.args.get("next")' in source, (
            "login() must validate 'next' with _safe_redirect() before storing in session"
        )

    def test_redirect_sinks_use_safe_redirect(self):
        """All redirect sinks that use login_next_url must go through _safe_redirect()."""
        from pathlib import Path

        source = Path("src/admin/blueprints/auth.py").read_text()
        # The unsafe pattern 'return redirect(next_url)' must not survive
        # after a raw session.pop("login_next_url", ...) without _safe_redirect.
        # We check that every pop of login_next_url is immediately wrapped.
        import re

        # Find raw pops not immediately followed by _safe_redirect
        raw_pop_then_redirect = re.search(
            r'session\.pop\("login_next_url".*?\)\s*\n\s*if next_url:\s*\n\s*return redirect\(next_url\)',
            source,
            re.MULTILINE,
        )
        assert raw_pop_then_redirect is None, (
            "Found an unguarded 'session.pop(login_next_url) → redirect(next_url)' pattern. "
            "Wrap the pop with _safe_redirect()."
        )
