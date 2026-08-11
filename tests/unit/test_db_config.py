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


class TestProductionSignalConverged:
    """The production signal converges onto config.is_production(), on a ratchet.

    Six call sites now delegate: scripts/run_server.py, src/core/auth.py,
    src/core/logging_config.py, src/core/audit_logger.py, src/admin/app.py, and
    src/admin/utils/helpers.py::is_admin_production. The first four used to test
    ``FLY_APP_NAME or PRODUCTION`` for bare presence, which silently treated
    PRODUCTION=false (an operator explicitly disabling it) as production and
    never consulted ENVIRONMENT at all -- the truthy-vocabulary bug
    is_production() already fixed (see _env_flag_is_true). The two admin sites
    compared PRODUCTION to a literal and so never saw FLY_APP_NAME, which left a
    Fly-only deployment serving its admin session cookie without Secure and
    leaving POST /test/auth reachable.

    Convergence is NOT complete, and this class does not claim it is. Eighteen
    open-coded production decisions remain, all in admin blueprints and the
    landing page. OPEN_CODED_ALLOWANCE is the shrink-only ratchet over exactly
    those: a new one anywhere fails immediately, and fixing one fails until its
    allowance is lowered, so the count cannot drift back up quietly. A prose
    claim of full convergence is what this replaces -- the previous guard
    checked one regex against four named files and could not have seen any of
    the eighteen.
    """

    # Every remaining site that reads a production signal directly, by file.
    # SHRINK ONLY: lower a number when you converge a site; never raise one.
    # Counts, not line numbers, so unrelated edits above a site do not break this.
    OPEN_CODED_ALLOWANCE = {
        # The definition itself -- is_production() and declares_production_explicitly()
        # are where these env vars are allowed to be read.
        "src/core/config.py": 2,
        # Not a production predicate: FLY_APP_NAME is consumed as a VALUE (the app
        # name) to build the Fly.io callback URL. Converging it would be wrong.
        "src/services/auth_config_service.py": 1,
        # FIXME(#1819): admin/landing sites still deciding production for
        # themselves. #1819 tracks routing the rest through is_production(); note
        # its premise that these sites are untouched by this PR no longer holds
        # for the ones converged here.
        #
        # app.py went 3 -> 1: besides the session-cookie policy, the two proxy
        # gates (PREFERRED_URL_SCHEME, and WerkzeugProxyFix/FlyHeadersMiddleware)
        # converged too. Those are NOT merely deploy-shape -- they decide whether
        # to TRUST forwarded proto headers, and on a Fly-only deploy the literal's
        # false branch left wsgi.url_scheme "http" while the cookie above it was
        # already marked Secure: the two halves of "are we behind an HTTPS proxy?"
        # disagreeing on one deployment.
        "src/admin/app.py": 1,
        "src/admin/blueprints/auth.py": 6,
        "src/admin/blueprints/authorized_properties.py": 4,
        "src/admin/blueprints/core.py": 1,
        "src/admin/blueprints/tenants.py": 1,
        "src/landing/landing_page.py": 3,
    }

    SIGNAL_READ = r'os\.(?:environ\.get|getenv)\(\s*"(?:PRODUCTION|ENVIRONMENT|FLY_APP_NAME)"'

    @classmethod
    def _open_coded_counts(cls):
        import re
        from collections import Counter
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        pattern = re.compile(cls.SIGNAL_READ)
        counts = Counter()
        for sub in ("src", "scripts"):
            for path in sorted((root / sub).rglob("*.py")):
                found = sum(1 for line in path.read_text().splitlines() if pattern.search(line))
                if found:
                    counts[path.relative_to(root).as_posix()] = found
        return counts

    def test_the_scanner_matches_a_known_open_coded_site(self):
        """An allowance table compared against a broken scanner passes by finding
        nothing, so the scanner must be shown to match a site that exists."""
        counts = self._open_coded_counts()
        assert counts.get("src/core/config.py"), "scanner found no signal read in config.py — regex is stale"

    def test_no_new_site_open_codes_the_production_signal(self):
        counts = self._open_coded_counts()
        new_files = sorted(set(counts) - set(self.OPEN_CODED_ALLOWANCE))
        assert not new_files, (
            f"{new_files} read PRODUCTION/ENVIRONMENT/FLY_APP_NAME directly — call "
            "src.core.config.is_production() instead of deciding production locally"
        )
        grew = {f: (n, self.OPEN_CODED_ALLOWANCE[f]) for f, n in counts.items() if n > self.OPEN_CODED_ALLOWANCE[f]}
        assert not grew, (
            f"open-coded production checks grew (file: found vs allowed): {grew} — this ratchet only shrinks"
        )

    def test_the_allowance_has_no_stale_entries(self):
        """A converged site whose allowance was left behind hides the next regression
        at that file, because the ratchet still has room for one."""
        counts = self._open_coded_counts()
        stale = {
            f: (counts.get(f, 0), allowed)
            for f, allowed in self.OPEN_CODED_ALLOWANCE.items()
            if counts.get(f, 0) < allowed
        }
        assert not stale, f"lower these allowances to what remains (file: found vs allowed): {stale}"

    def test_no_site_still_open_codes_the_bare_presence_check(self):
        """A regression back to a hand-rolled copy at any of the four sites must fail this test."""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        previously_open_coded = {
            "scripts/run_server.py",
            "src/core/auth.py",
            "src/core/logging_config.py",
            "src/core/audit_logger.py",
        }
        still_open_coded = {
            rel
            for rel in previously_open_coded
            if re.search(
                r'os\.environ\.get\("FLY_APP_NAME"\)[^\n]*os\.environ\.get\("PRODUCTION"\)',
                (root / rel).read_text(),
            )
        }
        assert not still_open_coded, f"reverted to the open-coded bare-presence check: {still_open_coded}"

    # The two security-bearing convergences are graded by BEHAVIOR, not by the
    # shape of this file's scan, because a source-shape assertion passes for a
    # rewrite that keeps the call and loses the effect:
    #   tests/unit/test_admin_session_cookie_policy.py — cookie Secure/SameSite
    #   tests/unit/test_test_auth_production_guard.py::
    #       test_fly_deployment_blocks_without_either_flag — the /test/auth 404
    # Both go red when their site reverts to comparing PRODUCTION to a literal.

    def test_is_production_correctly_handles_what_a_bare_presence_check_could_not(self, monkeypatch):
        """The two cases a naive bare-presence check would have gotten wrong."""
        from src.core.config import is_production

        def bare_presence(env: dict) -> bool:
            """What the four sites open-coded before converging onto is_production()."""
            return bool(env.get("FLY_APP_NAME") or env.get("PRODUCTION"))

        # PRODUCTION=false: is_production() honours the value; a presence check cannot.
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("FLY_APP_NAME", raising=False)
        monkeypatch.setenv("PRODUCTION", "false")
        assert is_production() is False
        assert bare_presence({"PRODUCTION": "false"}) is True, (
            "a naive bare-presence check would have treated an explicit PRODUCTION=false as production"
        )

        # ENVIRONMENT=production alone: production here, invisible to a presence check.
        monkeypatch.delenv("PRODUCTION", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "production")
        assert is_production() is True
        assert bare_presence({}) is False


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


class TestBddTransportTagSetsDoNotOverlap:
    """A tag in both routing sets makes its e2e_rest entry dead.

    pytest_generate_tests returns early for transport-independent scenarios, well
    before any e2e_rest exclusion or xfail is consulted. So an entry appearing in
    both sets excludes nothing — it reads as protection while being unreachable,
    which is how _NO_E2E_REST_TAGS' sole entry rotted after its scenario was
    routed, and how all eleven _UC004_E2E_WEBHOOK_INTERNAL_TAGS entries rotted
    after the UC-004 webhook scenarios were.

    Every set whose only use is gated on e2e_rest belongs in E2E_GATED_SETS. A
    set that is checked but absent from the table is the escape hatch this guard
    exists to close, so the table's own membership is pinned below.
    """

    # name -> anchor member proving the parser matched (None for a set that is
    # currently empty, which the parser must still resolve to an empty set
    # rather than to a parse failure).
    E2E_GATED_SETS = {
        "_NO_E2E_REST_TAGS": None,
        "_UC004_E2E_WEBHOOK_INTERNAL_TAGS": None,
        "_UC005_E2E_FIXTURE_INJECTION_TAGS": "T-UC-005-dim-boundary",
    }

    @staticmethod
    def _source():
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "bdd" / "conftest.py").read_text()

    @classmethod
    def _tags(cls, name, src=None):
        r"""Parse a tag set literal by balancing delimiters, not by line shape.

        A ``.*?\n\}`` regex only matches a brace literal closing at column 0, so
        it silently returns nothing for a function-local set (indented close) or
        for ``= set()``. Returning an empty set on a parse failure is exactly the
        false-green this guard must not have, so an unparseable name raises.
        """
        import re

        src = cls._source() if src is None else src
        m = re.search(rf"^\s*{re.escape(name)}\b[^=\n]*=\s*", src, re.M)
        assert m, f"{name} not found in conftest — guard is pointed at a renamed set"
        rest = src[m.end() :]
        opener = next((i for i, ch in enumerate(rest) if ch in "{("), None)
        assert opener is not None, f"{name} has no set/frozenset literal — parser is stale"
        depth, end = 0, None
        for i, ch in enumerate(rest[opener:], start=opener):
            if ch in "{(":
                depth += 1
            elif ch in "})":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        assert end is not None, f"{name} literal is unterminated — parser is stale"
        return set(re.findall(r'"([\w.-]+)"', rest[opener:end]))

    def test_the_parser_finds_the_transport_independent_set(self):
        """Guards that scan by regex must be shown to match, or an empty overlap is
        indistinguishable from a broken parser.

        Anchored on known members rather than a count. The original floor (">= 20")
        encoded the set's size at the moment it was written, so legitimately
        un-routing a dormant scenario broke a test that has nothing to do with the
        size — a guard that fails for the wrong reason trains people to adjust the
        number, which is exactly how a real staleness would then slip through.
        """
        transport_independent = self._tags("_TRANSPORT_INDEPENDENT_SCENARIO_TAGS")
        assert transport_independent, "parser found no routed tags — regex is stale"
        for anchor in ("T-UC-004-webhook-hmac", "T-UC-004-webhook-scheduled"):
            assert anchor in transport_independent, (
                f"parser did not find {anchor}, which is routed in conftest — regex is stale"
            )

    def test_the_parser_finds_each_e2e_gated_set(self):
        """A named anchor proves the balanced-delimiter parse works on the
        indented, function-local form the reachability check reads."""
        src = self._source()
        for name, anchor in self.E2E_GATED_SETS.items():
            tags = self._tags(name, src)  # raises if unparseable
            if anchor is not None:
                assert anchor in tags, f"parser did not find {anchor} in {name} — parser is stale"

    def test_every_e2e_gated_set_is_covered(self):
        """The table must name every set whose only use is behind ``is_e2e_rest``.

        Without this, adding a new gated set and forgetting the table entry
        reproduces the original defect with a green guard.
        """
        import re

        checked = set(re.findall(r"is_e2e_rest and \(marker_names & (\w+)\)", self._source()))
        checked.add("_NO_E2E_REST_TAGS")  # consulted as an exclusion, not an xfail
        missing = sorted(checked - set(self.E2E_GATED_SETS))
        assert not missing, (
            f"e2e_rest-gated sets {missing} are not in E2E_GATED_SETS, so their entries are "
            "never checked for reachability — add them to the table"
        )

    def test_e2e_gated_tags_are_reachable(self):
        src = self._source()
        transport_independent = self._tags("_TRANSPORT_INDEPENDENT_SCENARIO_TAGS", src)
        for name in self.E2E_GATED_SETS:
            dead = sorted(self._tags(name, src) & transport_independent)
            assert not dead, (
                f"{name} entries {dead} are also transport-independent, so the e2e_rest "
                "check never runs for them — remove the entry, or un-route the scenario if it "
                "must still parametrize across transports"
            )


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


class TestProductionConsequenceListIsComplete:
    """declares_production_explicitly's docstring enumerates what broadening reaches.

    That list is the only place an operator can see the upgrade risk, and it claims
    to be complete. It was written naming two of six consequences — the two
    loosenings — while omitting the SSRF tightening that can reject webhooks a
    deployment was successfully delivering. A prose completeness claim needs a
    mechanism, so this pins the module set rather than the wording.
    """

    EXPECTED_MODULES = {
        "config.py",
        "mcp_compat_middleware.py",
        "product_conversion.py",
        "webhook_validator.py",
    }

    @staticmethod
    def _modules_gating_on_is_production():
        import re
        from pathlib import Path

        src = Path(__file__).resolve().parents[2] / "src"
        found = set()
        for path in src.rglob("*.py"):
            body = path.read_text()
            # the call, not the import or the definition
            if re.search(r"\bis_production\(\)", body) and "def is_production" not in body.split("\n\n")[0]:
                if re.search(r"(?<!def )\bis_production\(\)", body):
                    found.add(path.name)
        return found

    def test_the_scan_finds_call_sites(self):
        assert self._modules_gating_on_is_production(), "scan found no is_production() callers — pattern stale"

    def test_every_module_gating_on_is_production_is_documented(self):
        from pathlib import Path

        doc = (Path(__file__).resolve().parents[2] / "src" / "core" / "config.py").read_text()
        start = doc.index("What the broadening actually reaches")
        # To the end of the enclosing docstring, not a fixed character budget: a
        # window sized to the prose as it stood silently truncates the moment the
        # enumeration grows, failing for a module that IS documented — the same
        # false verdict this guard exists to prevent, pointed the other way.
        end = doc.index('"""', start)
        listed_section = doc[start:end]

        actual = self._modules_gating_on_is_production()
        undocumented = sorted(m for m in actual if m.replace(".py", "") not in listed_section)
        assert not undocumented, (
            f"modules gate on is_production() but are absent from the enumeration: {undocumented} — "
            "an operator reading it would underestimate what changes on upgrade"
        )
