"""Tests for database configuration helpers."""

import ast
import re

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

    Convergence is NOT complete, and this class does not claim it is. Open-coded
    production decisions remain in admin blueprints and the landing page;
    OPEN_CODED_ALLOWANCE below is the authority on how many, and the shrink-only
    ratchet over exactly those: a new one anywhere fails immediately, and fixing
    one fails until its allowance is lowered, so the count cannot drift back up
    quietly. A prose claim of full convergence is what this replaces -- the
    previous guard checked one regex against four named files and could not have
    seen any of them.

    The count is deliberately NOT restated here. It is stated once, in
    config.py's docstring, and test_the_documented_remaining_count_matches_the_scan
    checks that one statement against the table.
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

    SIGNAL_NAMES = frozenset({"PRODUCTION", "ENVIRONMENT", "FLY_APP_NAME"})

    @staticmethod
    def _is_environ(node) -> bool:
        """``os.environ`` or a bare ``environ`` from ``from os import environ``."""
        return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
            isinstance(node, ast.Name) and node.id == "environ"
        )

    @classmethod
    def _reads_signal(cls, node) -> bool:
        """One read of a production signal env var, in any call shape.

        AST, not a regex. The previous SIGNAL_READ was a per-LINE match on
        ``os.(environ.get|getenv)("NAME")`` with a double quote hardcoded, so five
        real shapes were invisible to the ratchet and could be added freely:

            os.environ["PRODUCTION"]                 subscript
            os.getenv('PRODUCTION')                  single quotes
            from os import environ; environ.get(...) bare name
            os.environ.get(                          call split across lines
                "PRODUCTION")
            os.environ.get(KEY)                      non-literal key

        The first four are counted here. The fifth cannot be resolved statically
        and is reported separately by test_no_signal_is_read_through_an_indirect_key,
        because silently not counting it is how a site hides.
        """
        if isinstance(node, ast.Call):
            func = node.func
            is_getenv = (isinstance(func, ast.Attribute) and func.attr == "getenv") or (
                isinstance(func, ast.Name) and func.id == "getenv"
            )
            is_environ_get = isinstance(func, ast.Attribute) and func.attr == "get" and cls._is_environ(func.value)
            if (is_getenv or is_environ_get) and node.args:
                first = node.args[0]
                return isinstance(first, ast.Constant) and first.value in cls.SIGNAL_NAMES
            return False
        if isinstance(node, ast.Subscript) and cls._is_environ(node.value):
            key = node.slice
            return isinstance(key, ast.Constant) and key.value in cls.SIGNAL_NAMES
        return False

    @classmethod
    def _open_coded_counts(cls):
        from collections import Counter
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        counts = Counter()
        for sub in ("src", "scripts"):
            for path in sorted((root / sub).rglob("*.py")):
                found = sum(1 for node in ast.walk(ast.parse(path.read_text())) if cls._reads_signal(node))
                if found:
                    counts[path.relative_to(root).as_posix()] = found
        return counts

    @pytest.mark.parametrize(
        "source,why",
        [
            pytest.param('os.environ.get("PRODUCTION")', "the shape the old regex matched", id="dotted-get"),
            pytest.param("os.getenv('PRODUCTION')", "single quotes", id="single-quoted"),
            pytest.param('os.environ["PRODUCTION"]', "subscript", id="subscript"),
            pytest.param("environ.get('FLY_APP_NAME')", "from os import environ", id="bare-environ-get"),
            pytest.param('environ["ENVIRONMENT"]', "from os import environ, subscript", id="bare-environ-sub"),
            pytest.param('os.environ.get(\n    "PRODUCTION"\n)', "call split across lines", id="multiline"),
            pytest.param('getenv("PRODUCTION")', "from os import getenv", id="bare-getenv"),
        ],
    )
    def test_the_scanner_sees_every_read_shape(self, source, why):
        """Each shape below was green under the old per-line regex except the first.

        Parametrized rather than spot-checked: the defect was that ONE shape was
        modelled, so a test asserting only that shape agrees with the bug.
        """
        found = [n for n in ast.walk(ast.parse(source)) if self._reads_signal(n)]
        assert len(found) == 1, f"scanner missed {why}: {source!r}"

    @pytest.mark.parametrize(
        "source",
        [
            'os.environ.get("UNRELATED_VAR")',
            'some_dict.get("PRODUCTION")',
            'config["PRODUCTION"]',
        ],
    )
    def test_the_scanner_does_not_over_match(self, source):
        """A scanner that counts any .get("PRODUCTION") would flag unrelated dicts,
        and a ratchet that cries wolf gets its allowance raised rather than read."""
        assert not [n for n in ast.walk(ast.parse(source)) if self._reads_signal(n)], source

    # Generic env accessors that take the variable NAME as a parameter. These are
    # not open-coded production decisions -- the caller chooses the variable, and a
    # caller passing a signal name does so as a literal the ratchet still sees at
    # its own site. Forbidding the pattern outright would ban a normal idiom and
    # get this test's allowance raised rather than read.
    INDIRECT_KEY_ALLOWLIST = {
        "src/core/config.py": "_env_flag_is_true(name) -- the shared truthy-vocabulary parser",
        "src/core/database/db_config.py": "int_env(name, default) -- generic integer env parser",
        "src/core/config_loader.py": "get_secret(key, default) -- generic secret accessor",
        "src/admin/auth_helpers.py": "env-or-config lookup taking env_var as a parameter",
    }

    def test_no_signal_is_read_through_an_indirect_key(self):
        """``os.environ[KEY]`` cannot be resolved statically, so a NEW one must not appear.

        The one shape the AST scanner genuinely cannot count: if the key is a
        variable, no static check can tell whether it holds "PRODUCTION". Left
        entirely unchecked that is an unmonitored way to open-code the signal, so
        new sites are refused while the four generic accessors are named above.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        indirect = []
        for sub in ("src", "scripts"):
            for path in sorted((root / sub).rglob("*.py")):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    dynamic_sub = (
                        isinstance(node, ast.Subscript)
                        and self._is_environ(node.value)
                        and not isinstance(node.slice, ast.Constant)
                    )
                    dynamic_get = (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "get"
                        and self._is_environ(node.func.value)
                        and node.args
                        and not isinstance(node.args[0], ast.Constant)
                    )
                    if (dynamic_sub or dynamic_get) and path.relative_to(root).as_posix() not in (
                        self.INDIRECT_KEY_ALLOWLIST
                    ):
                        indirect.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
        assert not indirect, (
            f"environ read through a non-literal key at {indirect} — the ratchet cannot see these, "
            "so a production signal could be open-coded there unmonitored. Use a literal name, or "
            "add the file to INDIRECT_KEY_ALLOWLIST if it is a generic accessor taking the name as "
            "a parameter."
        )

    def test_the_indirect_key_allowlist_has_no_stale_entries(self):
        """An allowlisted file that no longer reads environ indirectly hides the next one."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        stale = []
        for rel in self.INDIRECT_KEY_ALLOWLIST:
            tree = ast.parse((root / rel).read_text())
            has_indirect = any(
                (isinstance(n, ast.Subscript) and self._is_environ(n.value) and not isinstance(n.slice, ast.Constant))
                or (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "get"
                    and self._is_environ(n.func.value)
                    and n.args
                    and not isinstance(n.args[0], ast.Constant)
                )
                for n in ast.walk(tree)
            )
            if not has_indirect:
                stale.append(rel)
        assert not stale, f"remove these from INDIRECT_KEY_ALLOWLIST — they no longer read environ indirectly: {stale}"

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

    # SITES the ratchet allows must each carry their FIXME. CLAUDE.md requires a
    # `# FIXME(#<issue>)` at the SOURCE location of every allowlisted violation, and
    # the markers were placed once in this table instead -- where the next person
    # editing src/admin/blueprints/auth.py cannot see them. Placing them was an
    # instance fix; this is what stops them drifting apart again, since a new site
    # added with an allowance bump and no marker would otherwise pass.
    FIXME_MARKER = "FIXME(#1819)"

    # Files whose allowance is NOT an open-coded violation awaiting convergence, so
    # a FIXME there would be wrong. Reasons mirror the table's own comments.
    FIXME_EXEMPT = {
        "src/core/config.py": "the definition itself -- these reads are where the signal is allowed",
        "src/services/auth_config_service.py": "FLY_APP_NAME consumed as a value, not as a production predicate",
    }

    def test_every_allowlisted_site_carries_its_fixme_marker(self):
        """Marker count must EQUAL the allowance, per file.

        Equality, not presence: one marker on a six-site file reads as "this file is
        tracked" while five sites stay unannotated, which is the same
        looks-covered-but-isn't shape the allowlist exists to prevent.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        mismatched = {}
        for rel, allowed in self.OPEN_CODED_ALLOWANCE.items():
            if rel in self.FIXME_EXEMPT:
                continue
            markers = (root / rel).read_text().count(self.FIXME_MARKER)
            if markers != allowed:
                mismatched[rel] = {"markers": markers, "allowed": allowed}
        assert not mismatched, (
            f"{self.FIXME_MARKER} markers do not match the allowance (file: markers vs allowed): "
            f"{mismatched} -- CLAUDE.md requires one at each allowlisted source location, so a "
            "reader of that file learns the site is tracked without consulting this table"
        )

    def test_every_fixme_marker_sits_at_its_site(self):
        """PLACEMENT, not just count.

        The count check above is satisfied by six markers anywhere in a six-site
        file — all of them in the module docstring, say — while its own message
        claims location semantics ("at each allowlisted source location, so a reader
        of that file learns the site is tracked"). CLAUDE.md asks for the marker AT
        the site; an assertion that cannot tell a marker at line 400 from one above
        line 12 is not checking that.

        Proximity, not exact adjacency: a marker may carry a wrapped explanation, so
        requiring line N-1 exactly would break on a two-line note. Within three lines
        above keeps it attached to the site while leaving room to explain.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        detached = {}
        for rel in self.OPEN_CODED_ALLOWANCE:
            if rel in self.FIXME_EXEMPT:
                continue
            lines = (root / rel).read_text().splitlines()
            marker_lines = [i for i, line in enumerate(lines) if self.FIXME_MARKER in line]
            tree = ast.parse("\n".join(lines))
            site_lines = sorted({n.lineno - 1 for n in ast.walk(tree) if self._reads_signal(n)})
            unmarked = [ln + 1 for ln in site_lines if not any(0 < ln - m <= 3 for m in marker_lines)]
            if unmarked:
                detached[rel] = unmarked
        assert not detached, (
            f"{self.FIXME_MARKER} is not within three lines above these sites (file: line numbers): "
            f"{detached} -- the marker exists to be seen by whoever edits that line, so its position "
            "is the whole point"
        )

    def test_the_fixme_exemptions_still_have_allowances(self):
        """An exemption for a file that left the table hides the next real violation."""
        orphaned = sorted(set(self.FIXME_EXEMPT) - set(self.OPEN_CODED_ALLOWANCE))
        assert not orphaned, f"these files are FIXME-exempt but no longer allowlisted: {orphaned}"

    def test_the_documented_remaining_count_matches_the_scan(self):
        """config.py's stated count must equal the table's, and the scanner's.

        Three defects in the first version of this test, all of which made it
        weaker than it looked:

        * It grepped for an English number word ("sixteen") in THIS file, while the
          word came from a ``{10: "ten", ...}`` table of literals declared a few
          lines above the assertion — so the "this file" half matched itself and
          could never fail.
        * ``assert word, "extend the number-word table"`` went RED when the count
          dropped below 10, i.e. the test failed because the ratchet IMPROVED, and
          the remedy was to edit the test.
        * The config.py half was a bare substring check, so "ten" matched inside
          "Tightening".

        Now: the count lives in exactly ONE prose location (config.py's docstring),
        is written as a digit, and is matched with word boundaries so no substring
        can satisfy it. Any integer works, so shrinking the ratchet can never fail
        this test for improving.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        remaining = sum(n for f, n in self.OPEN_CODED_ALLOWANCE.items() if f not in self.FIXME_EXEMPT)

        scanned = sum(n for f, n in self._open_coded_counts().items() if f not in self.FIXME_EXEMPT)
        assert scanned == remaining, (
            f"the allowance table totals {remaining} un-converged sites but the scanner finds "
            f"{scanned} -- the table is stale"
        )

        doc = (root / "src" / "core" / "config.py").read_text()
        stated = re.findall(r"\b(\d+) open-coded production decisions remain\b", doc)
        assert stated == [str(remaining)], (
            f"config.py states {stated or 'no'} open-coded production decisions remain, but the "
            f"allowance table and the scanner both say {remaining}. config.py's docstring is the "
            "single place this count is written -- update it there."
        )

    def test_the_count_is_stated_in_exactly_one_place(self):
        """A number restated in two files is the falsifiable comment this removes.

        The count had drifted three ways at once (this file said "Eighteen",
        config.py "sixteen", the scanner 16) precisely because two files asserted
        it independently. Keeping it to one statement is what makes the check above
        meaningful rather than a race between two prose sources.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        pattern = re.compile(r"\b\d+ open-coded production decisions remain\b")
        stating = [
            rel
            for rel in ("src/core/config.py", "tests/unit/test_db_config.py")
            if pattern.search((root / rel).read_text())
        ]
        assert stating == ["src/core/config.py"], (
            f"the remaining-count is stated in {stating}; it belongs in config.py's docstring only, "
            "so there is one thing to keep true"
        )

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


# REMOVED: TestPinnedErrorEnumIsNotACanonicityGate.
#
# It guarded a workaround, and the workaround's premise is gone. The class
# existed because tests/harness/transport.py sourced error-code metadata from
# the vendored fixture tree pinned at @04f59d2d5, which carried 64 codes against
# the 92 released at the targeted v3.1.1. Treating absence there as "not a
# canonical code" would have rejected 28 codes that ARE canonical, so the
# harness deliberately did not, and these tests pinned that leniency.
#
# #1868 repointed the harness at the SDK's own schema tree, where the installed
# adcp version IS the pin. Absence from that enum now genuinely means the code is
# not canonical, so the harness asserts it -- and the leniency these tests
# required is exactly what upstream removed on purpose. Keeping them would pin a
# behavior the codebase no longer has, against a fixture the harness no longer
# reads.


class TestBddE2eGatedSetsAreEnumerated:
    """Every set whose only use is gated on ``is_e2e_rest`` is named in E2E_GATED_SETS.

    A set that is checked in conftest.py but absent from the table is the escape
    hatch this guard exists to close, so the table's own membership is derived from
    the source and pinned below, and the parser that reads each set is shown to
    match rather than silently returning nothing.

    Reachability of the entries themselves — whether the gate they sit behind can
    ever be consulted — is NOT checked here. The only mechanism this file ever had
    for that was an overlap test against a transport-independent exemption registry
    that has since been deleted, and an intersection with a set asserted elsewhere
    to be empty proves nothing. An AST-derived replacement covering both deadness
    mechanisms is tracked separately.
    """

    # name -> anchor member proving the parser matched (None for a set that is
    # currently empty, which the parser must still resolve to an empty set
    # rather than to a parse failure). Every set here is populated, so every
    # entry carries a real anchor: a None would let a silently-broken parse
    # count as a pass.
    E2E_GATED_SETS = {
        "_NO_E2E_REST_TAGS": "T-UC-004-webhook-ssrf-blocked",
        "_UC004_E2E_WEBHOOK_INTERNAL_TAGS": "T-UC-004-webhook-sequence",
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

    def test_the_parser_finds_a_populated_set(self):
        """Guards that scan by regex must be shown to match, or an empty result is
        indistinguishable from a broken parser.

        Anchored on a POPULATED set. The parser is shared (``_tags``), so proving it
        against any populated set proves it for all of them — and anchoring the
        liveness check on a set that is supposed to stay empty would mean re-pointing
        this test every time an exemption set is correctly drained, which is the
        "guard fails for the wrong reason" shape its predecessor's docstring already
        warned about.
        """
        populated = self._tags("_UC026_XFAIL_TAGS")
        assert populated, "parser found no tags in _UC026_XFAIL_TAGS — regex is stale"
        assert "T-UC-026-main-full-config" in populated, (
            "parser did not find a known _UC026_XFAIL_TAGS member — regex is stale"
        )

    def test_the_parser_finds_each_e2e_gated_set(self):
        """A named anchor proves the balanced-delimiter parse works on the
        indented, function-local form these sets are declared in."""
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
            f"e2e_rest-gated sets {missing} are not in E2E_GATED_SETS, so nothing enumerates "
            "them — add them to the table"
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

    @staticmethod
    def _modules_gating_on_is_production():
        import re
        from pathlib import Path

        src = Path(__file__).resolve().parents[2] / "src"
        found = set()
        for path in src.rglob("*.py"):
            # CODE only. Scanning the raw text counted a module that merely NAMES
            # is_production() in a comment as one that GATES on it, which is a
            # different claim -- the enumeration this feeds documents what changes
            # behaviorally on upgrade, and a comment changes nothing. Surfaced by
            # the FIXME(#1819) annotations, which say "route through
            # src.core.config.is_production()" at each open-coded site: those sites
            # are precisely the ones NOT gating on it yet.
            #
            # Line-level comment stripping, not tokenize: a `#` inside a string
            # literal would truncate that line, which can only ever LOSE a match
            # here, and every real call site is a bare statement or condition.
            body = "\n".join(line.split("#", 1)[0] for line in path.read_text().splitlines())
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
