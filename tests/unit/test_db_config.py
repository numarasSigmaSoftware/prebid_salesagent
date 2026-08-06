"""Tests for database configuration helpers."""

import pytest

from src.core.database.db_config import int_env


class TestIntEnv:
    """int_env parses integer environment variables with friendly errors."""

    def test_returns_int_for_valid_env(self, monkeypatch):
        monkeypatch.setenv("TEST_INT_ENV", "42")
        assert int_env("TEST_INT_ENV", "0") == 42

    def test_returns_default_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("TEST_INT_ENV", raising=False)
        assert int_env("TEST_INT_ENV", "7") == 7

    def test_returns_default_when_env_empty(self, monkeypatch):
        monkeypatch.setenv("TEST_INT_ENV", "")
        assert int_env("TEST_INT_ENV", "9") == 9

    def test_raises_for_invalid_value(self, monkeypatch):
        monkeypatch.setenv("TEST_INT_ENV", "not-a-number")
        with pytest.raises(ValueError, match="Invalid integer value for TEST_INT_ENV"):
            int_env("TEST_INT_ENV", "0")


class TestProductionSignalDivergence:
    """config.is_production() is a strict superset of the bare-presence checks.

    Four call sites decide "are we in production?" independently of this function
    -- scripts/run_server.py, src/core/auth.py, src/core/logging_config.py and
    src/core/audit_logger.py -- each by testing ``FLY_APP_NAME or PRODUCTION`` for
    bare presence. The docstring on is_production() once claimed it matched their
    union. It does not, and the disagreement is operator-visible, so it is pinned
    here rather than described in prose.
    """

    @staticmethod
    def _bare_presence(env) -> bool:
        """The shape all four sites open-code today."""
        return bool(env.get("FLY_APP_NAME") or env.get("PRODUCTION"))

    def test_the_four_sites_still_open_code_the_bare_presence_check(self):
        """If a site converges onto is_production(), this test must be revisited --
        it is the trigger for re-deciding the PRODUCTION=false behavior change,
        not a decoration."""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        expected = {
            "scripts/run_server.py",
            "src/core/auth.py",
            "src/core/logging_config.py",
            "src/core/audit_logger.py",
        }
        found = {
            rel
            for rel in expected
            if re.search(
                r'os\.environ\.get\("FLY_APP_NAME"\)[^\n]*os\.environ\.get\("PRODUCTION"\)',
                (root / rel).read_text(),
            )
        }
        assert found == expected, f"bare-presence check moved or converged in: {expected - found}"

    def test_is_production_diverges_from_the_bare_presence_checks(self, monkeypatch):
        """The two disagreeing cases, stated as behavior rather than prose."""
        from src.core.config import is_production

        # PRODUCTION=false: this function honours the value; a presence check cannot.
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("FLY_APP_NAME", raising=False)
        monkeypatch.setenv("PRODUCTION", "false")
        env = {"PRODUCTION": "false"}
        assert is_production() is False
        assert self._bare_presence(env) is True, (
            "an operator who explicitly disables PRODUCTION still gets production "
            "auth/logging/audit behavior from the four open-coded sites"
        )

        # ENVIRONMENT=production alone: production here, invisible to a presence check.
        monkeypatch.delenv("PRODUCTION", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "production")
        assert is_production() is True
        assert self._bare_presence({}) is False


class TestPinnedErrorEnumIsNotACanonicityGate:
    """The vendored error-code enum supplies recovery; it does not decide existence.

    It is pinned at @04f59d2d5 and carries 64 codes against the 92 released at the
    targeted v3.1.1. Using absence as proof of non-canonicity rejected 28 codes that
    are canonical at the version this repo targets, with a message telling the author
    to change a correct code.
    """

    def _assert_shape(self, code, recovery):
        from tests.harness.transport import TransportResult

        envelope = {
            "adcp_error": {"code": code, "message": "boom", "recovery": recovery},
            "errors": [{"code": code, "message": "boom", "recovery": recovery, "suggestion": "s"}],
        }
        TransportResult._assert_error_envelope(
            TransportResult(payload=None),
            envelope,
            code,
            source="wire",
            recovery=recovery,
            require_suggestion=False,
            message_substr=None,
        )

    def test_a_code_absent_from_the_vendored_enum_is_accepted_with_explicit_recovery(self):
        """PROPOSAL_NOT_FOUND ships in the installed SDK for the targeted version but
        is missing from the vendored tree — precisely the 28-code gap."""
        import json
        from pathlib import Path

        pinned = json.loads(
            (
                Path(__file__).resolve().parents[1] / "fixtures" / "adcp_schemas_pinned" / "enums" / "error-code.json"
            ).read_text()
        )["enumMetadata"]
        assert "PROPOSAL_NOT_FOUND" not in pinned, "fixture refreshed — pick another gap code"

        self._assert_shape("PROPOSAL_NOT_FOUND", "terminal")  # must not raise

    def test_a_code_absent_from_the_vendored_enum_still_demands_an_explicit_recovery(self):
        """The fixture cannot supply recovery for a code it does not carry, so the
        caller must state it rather than silently inherit an unknown value."""
        import pytest

        with pytest.raises(AssertionError, match="Pass recovery= explicitly"):
            self._assert_shape("PROPOSAL_NOT_FOUND", None)


class TestBddDispatchersShareOneResultRecorder:
    """Every BDD dispatcher must route ctx-writing through record_transport_result.

    The mapping from TransportResult to ctx keys has exactly one home. A dispatcher
    that writes its own keys does not fail loudly when it omits one: envelope
    consumers read ctx.get("wire_error_envelope") behind an `if isinstance(...,
    dict)` guard, so a missing key skips the assertion block and the step passes
    having graded nothing. Delegation makes that unreachable; this keeps it true.
    """

    DISPATCHERS = ("bdd/steps/generic/_dispatch.py", "bdd/steps/generic/when_request.py")
    RECORDER_KEYS = {
        "result",
        "error",
        "wire_error_envelope",
        "synthesized_error_envelope",
        "response",
        "wire_response",
    }

    @staticmethod
    def _source(rel):
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / rel).read_text()

    @staticmethod
    def _ctx_keys(src):
        import re

        return set(re.findall(r'ctx\["(\w+)"\]\s*=', src))

    def _recorder_body(self):
        src = self._source("bdd/steps/generic/_dispatch.py")
        body = src[src.index("def record_transport_result(") :]
        return body.split("\ndef ")[0]

    def test_the_recorder_writes_every_ctx_key(self):
        """Pins the premise: uniform delegation to a recorder that dropped a key
        would be consistent and still wrong."""
        assert self._ctx_keys(self._recorder_body()) == self.RECORDER_KEYS

    def test_no_dispatcher_writes_recorder_keys_itself(self):
        # "error" is deliberately exempt: the recorder sets it from result.error,
        # but a dispatcher's `except Exception as exc: ctx["error"] = exc` records a
        # RAISED exception, which never had a TransportResult to derive from. Both
        # writes are legitimate and neither can be routed through the other. The
        # remaining five are result-derived and have exactly one source.
        recorder_owned = self.RECORDER_KEYS - {"error"}
        recorder_body = self._recorder_body()
        offenders = {}
        for rel in self.DISPATCHERS:
            src = self._source(rel)
            assert "record_transport_result" in src, f"{rel} does not delegate to the shared recorder"
            hand_rolled = self._ctx_keys(src.replace(recorder_body, "")) & recorder_owned
            if hand_rolled:
                offenders[rel] = sorted(hand_rolled)
        assert not offenders, (
            f"dispatchers writing recorder-owned ctx keys directly: {offenders} — route them "
            "through record_transport_result so a partial write cannot pass silently"
        )
