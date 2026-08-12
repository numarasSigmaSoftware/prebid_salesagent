"""A non-JSON value in ``context`` must not cost the buyer their error envelope.

CPython's ``json.loads`` accepts the JSON-non-standard literals ``NaN`` /
``Infinity`` / ``-Infinity`` by default, so a buyer can put a real non-finite
float into the opaque ``context`` object. The error boundary echoes ``context``
back on the envelope — and an echo of a non-finite float is something no JSON
encoder will emit:

    json.loads('{"x": NaN}')                       -> {'x': nan}     (accepted)
    JSONResponse(400, content={'context': ...})    -> ValueError: Out of range
                                                      float values are not JSON
                                                      compliant: nan

Because that raise happens INSIDE the exception handler, it shadows the buyer's
real error and the boundary emits no envelope at all — the buyer gets nothing
back for a request that should have produced a clean, well-formed error.

This drives the real HTTP wire, so it fails if the coercion regresses anywhere
between the serializer and the encoder — not merely if
``serialize_application_context`` changes shape. That, plus the MCP and A2A
envelope arms, is pinned in ``tests/unit/test_application_context_serialization.py``.

Spec: pinned AdCP 3.1.1 ``core/context.json`` types ``context`` as a bare opaque
object ("echoed unchanged", "never parsed by AdCP agents"). Exact echo is
undefined for a value JSON cannot represent, so the non-finite float is echoed
as JSON ``null`` — the key stays present and the envelope stays emittable.
Ungraded: no 3.1.1 conformance storyboard step covers a non-JSON context value.
"""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

# Raw bytes, not a dict: the point is that this is what a buyer can actually put
# on the wire, and that our own parser accepts it.
_NAN_CONTEXT_BODY = b'{"paused": true, "context": {"x": NaN}}'


def _put_with_non_json_context():
    """PUT the raw body through the real middleware stack.

    No ``with`` block: the app's lifespan starts schedulers and can only be
    entered once per process, and none of it is needed to reach the exception
    handler — the auth rejection happens in middleware. Same construction as
    ``test_bearer_across_transports.py``.
    """
    from starlette.testclient import TestClient

    from src.app import app

    return TestClient(app).put(
        "/api/v1/media-buys/mb-1",
        content=_NAN_CONTEXT_BODY,
        headers={"content-type": "application/json"},
    )


class TestRestBoundaryEmitsAnEnvelopeForNonJsonContext:
    """Unauthenticated is deliberate: it is the shortest path to the exception
    handler, needs no fixtures, and proves the fault is reachable by anyone who
    can reach the port."""

    def test_unauthenticated_put_with_nan_context_still_returns_its_auth_error(self, integration_db):
        """The pre-fix failure mode was no response at all, not a wrong response."""
        response = _put_with_non_json_context()

        assert response.status_code == 401, f"expected the auth error, got {response.status_code}"
        body = response.json()
        assert body["adcp_error"]["code"] == "AUTH_REQUIRED"
        assert body["errors"][0]["code"] == "AUTH_REQUIRED"

    def test_the_non_finite_value_is_echoed_as_null_not_dropped(self, integration_db):
        """Context echo is positional as well as value-wise: the key must survive."""
        echoed = _put_with_non_json_context().json()["context"]

        assert echoed == {"x": None}, f"expected the key echoed as null, got {echoed!r}"

    def test_the_http_body_is_readable_by_a_strict_json_parser(self, integration_db):
        """A conformance runner parses the raw bytes, and it will not accept ``NaN``."""

        def _reject(constant: str) -> None:
            raise AssertionError(f"non-JSON constant {constant!r} reached the REST wire")

        json.loads(_put_with_non_json_context().content, parse_constant=_reject)


class TestRestBoundaryNeverGoesSilentOnAnUnencodableEnvelope:
    """The last-resort guard in ``_envelope_response``, graded directly.

    With the serializer coercing every context value, nothing unencodable can
    reach ``JSONResponse`` today — so this guard is unreachable by construction
    and would otherwise ship as untested defensive code. The failure it prevents
    is total silence (a raise inside an exception handler replaces the buyer's
    error with no response at all), which is severe enough to keep the guard;
    keeping it means grading it, so the unencodable value is injected here.
    """

    def test_a_raising_envelope_BUILDER_still_yields_the_error(self, monkeypatch, integration_db):
        """The builder, not just the encoder, is inside the guard.

        `build_two_layer_error_envelope` walks the buyer's context and is the
        likelier of the two calls to raise. It sat one line ABOVE the try, so
        the guard covered only `JSONResponse` and a raise in the builder still
        produced no response at all — the exact failure the guard exists for.
        """
        import src.app as app_module

        real_builder = app_module.build_two_layer_error_envelope
        calls = {"n": 0}

        def _raises_once(exc):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("builder blew up on the buyer's context")
            return real_builder(exc)

        monkeypatch.setattr(app_module, "build_two_layer_error_envelope", _raises_once)

        response = _put_with_non_json_context()

        assert response.status_code == 401, "a raising builder must not cost the buyer their error"
        assert response.json()["adcp_error"]["code"] == "AUTH_REQUIRED"
        assert calls["n"] == 2, "the guard should have retried the build without the context"

    def test_an_unencodable_envelope_still_yields_the_error_without_the_echo(self, monkeypatch, integration_db):
        import src.app as app_module

        real_builder = app_module.build_two_layer_error_envelope

        def _unencodable(exc):
            envelope = real_builder(exc)
            envelope["context"] = {"x": float("nan")}  # bypasses the serializer entirely
            return envelope

        monkeypatch.setattr(app_module, "build_two_layer_error_envelope", _unencodable)

        response = _put_with_non_json_context()

        assert response.status_code == 401, "the buyer's real error must survive an unencodable echo"
        body = response.json()
        assert body["adcp_error"]["code"] == "AUTH_REQUIRED"
        assert "context" not in body, "the unencodable echo must be dropped, not emitted"
