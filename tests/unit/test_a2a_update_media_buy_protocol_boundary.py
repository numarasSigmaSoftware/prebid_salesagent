"""Real A2A-wire regressions for update_media_buy request guards."""

from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from src.app import app
from tests.factories.principal import PrincipalFactory
from tests.helpers import assert_envelope_shape
from tests.unit.test_a2a_transport_contract import (
    _build_jsonrpc,
    _extract_artifact_data,
    _extract_jsonrpc_result,
)

_MOCK_IDENTITY = PrincipalFactory.make_identity(
    principal_id="update-boundary-principal",
    tenant_id="update-boundary-tenant",
    tenant={"tenant_id": "update-boundary-tenant"},
    protocol="a2a",
)


@pytest.mark.parametrize(
    ("parameters", "code", "message"),
    [
        pytest.param(
            {"media_buy_id": "mb-1", "paused": True},
            "VALIDATION_ERROR",
            "idempotency_key is required",
            id="omitted-idempotency-key",
        ),
        pytest.param(
            {"media_buy_id": "mb-1", "paused": True, "idempotency_key": 123},
            "VALIDATION_ERROR",
            "idempotency_key must be a string",
            id="non-string-idempotency-key",
        ),
        pytest.param(
            {
                "media_buy_id": "mb-1",
                "paused": True,
                "idempotency_key": "a2a-update-key-0001",
                "revision": 7,
            },
            # A2A's JSON-RPC/protobuf layer coerces the integer 7 to a float, but
            # the guard classifies on numeric VALUE (not Python type), so this
            # converges with MCP/REST on UNSUPPORTED_FEATURE — a schema-valid
            # revision names a field this seller does not implement.
            "UNSUPPORTED_FEATURE",
            "does not support optimistic-concurrency control",
            id="unsupported-revision",
        ),
        pytest.param(
            {
                "media_buy_id": "mb-1",
                "paused": True,
                "idempotency_key": "a2a-update-key-0001",
                "revision": 0,
            },
            # Below minimum:1 is schema-invalid -> INVALID_REQUEST (BR-UC-003 below_min).
            "INVALID_REQUEST",
            "must be an integer",
            id="below-minimum-revision",
        ),
    ],
)
def test_update_media_buy_rejects_invalid_protocol_fields_before_core_call(
    parameters: dict, code: str, message: str
) -> None:
    """A2A exposes buyer-correctable envelopes and never invokes the core write."""
    with (
        patch("src.core.resolved_identity.resolve_identity", return_value=_MOCK_IDENTITY),
        patch("src.core.tools.media_buy_update._update_media_buy_impl") as mock_core,
    ):
        client = TestClient(app, raise_server_exceptions=False)
        try:
            response = client.post(
                "/a2a",
                json=_build_jsonrpc("update_media_buy", parameters),
                headers={
                    "Authorization": "Bearer update-boundary-token",
                    "Content-Type": "application/json",
                    "A2A-Version": "1.0",
                },
            )
        finally:
            client.close()

    assert response.status_code == 200
    data = _extract_artifact_data(_extract_jsonrpc_result(response))
    assert_envelope_shape(data, code, recovery="correctable", message_substr=message)
    mock_core.assert_not_called()


def test_update_media_buy_accepts_omitted_revision_before_core_call() -> None:
    """Omitted revision proceeds to core, proving the real A2A path preserves omission."""
    from src.core.schemas import UpdateMediaBuyRequest, UpdateMediaBuyResult, UpdateMediaBuySuccess

    success = UpdateMediaBuyResult(
        # carrier(), not sync_success(): this stands in for a mocked _impl return, so it
        # does not speak for the repository and has no persisted revision to report.
        response=UpdateMediaBuySuccess.carrier(media_buy_id="mb-1", affected_packages=[]),
        status="completed",
    )
    with (
        patch("src.core.resolved_identity.resolve_identity", return_value=_MOCK_IDENTITY),
        patch("src.core.tools.media_buy_update._update_media_buy_impl", return_value=success) as mock_core,
    ):
        client = TestClient(app, raise_server_exceptions=False)
        try:
            response = client.post(
                "/a2a",
                json=_build_jsonrpc(
                    "update_media_buy",
                    {
                        "media_buy_id": "mb-1",
                        "paused": True,
                        "idempotency_key": "a2a-update-key-0001",
                    },
                ),
                headers={
                    "Authorization": "Bearer update-boundary-token",
                    "Content-Type": "application/json",
                    "A2A-Version": "1.0",
                },
            )
        finally:
            client.close()

    assert response.status_code == 200
    data = _extract_artifact_data(_extract_jsonrpc_result(response))
    assert data["media_buy_id"] == "mb-1"
    mock_core.assert_called_once_with(
        req=UpdateMediaBuyRequest(
            media_buy_id="mb-1",
            paused=True,
            idempotency_key="a2a-update-key-0001",
        ),
        identity=_MOCK_IDENTITY,
        context_id=None,
    )


def test_update_media_buy_rejects_explicit_null_revision_before_core_call() -> None:
    """Explicit JSON null is schema-invalid, not a spelling of omission — A2A parity with MCP/REST.

    At the pinned adcp 6.6.0, an unset ``revision`` is OMITTED from
    ``UpdateMediaBuyRequest.model_dump()`` — no conformant client emits null —
    and ``update-media-buy-request.json`` (v3.1.1) types the field
    ``{type: integer, minimum: 1}``, which null violates.
    """
    with (
        patch("src.core.resolved_identity.resolve_identity", return_value=_MOCK_IDENTITY),
        patch("src.core.tools.media_buy_update._update_media_buy_impl") as mock_core,
    ):
        client = TestClient(app, raise_server_exceptions=False)
        try:
            response = client.post(
                "/a2a",
                json=_build_jsonrpc(
                    "update_media_buy",
                    {
                        "media_buy_id": "mb-1",
                        "paused": True,
                        "idempotency_key": "a2a-update-key-0001",
                        "revision": None,
                    },
                ),
                headers={
                    "Authorization": "Bearer update-boundary-token",
                    "Content-Type": "application/json",
                    "A2A-Version": "1.0",
                },
            )
        finally:
            client.close()

    assert response.status_code == 200
    data = _extract_artifact_data(_extract_jsonrpc_result(response))
    assert_envelope_shape(data, "INVALID_REQUEST", recovery="correctable", message_substr="must be an integer")
    mock_core.assert_not_called()


def test_a2a_data_part_encoding_loses_integer_typing() -> None:
    """Characterize the A2A-only integer loss on the outbound wire.

    This is a PIN of current transport behaviour, not an endorsement of it.
    ``_restore_a2a_integer_version_pin`` repairs integers on the way IN; there
    is no outbound counterpart because ``google.protobuf.Value`` has exactly
    one numeric field, ``number_value``, typed ``double``. Every JSON integer
    handed to ``_dict_to_value`` is therefore emitted as ``3.0`` at any nesting
    depth, while ``bool`` (a distinct ``bool_value``) survives.

    Divergences this records, against the pinned adcp 6.6.0 / AdCP 3.1.1:

    * ``error-details/version-unsupported.json`` types ``supported_majors``
      items as ``{type: integer, minimum: 1}``, so A2A buyers see ``[3.0]``
      where the schema says ``[3]``. Impact is bounded: that same schema
      documents the field as "DEPRECATED in favor of ``supported_versions``
      ... removed in 4.0", and ``supported_versions`` is string-typed and so
      unaffected.
    * The more serious case is the echoed application ``context``. AdCP L2
      ``context-sessions.mdx`` rule 5 forbids an agent to "add, remove,
      rename, reorder, or retype" echoed context, and int -> float IS a
      retype — of buyer-supplied values this agent never inspects, at
      arbitrary depth.

    If protobuf ever grows integer-preserving encoding, or someone lands an
    outbound repair, this test goes RED and should be deleted along with the
    docstrings it anchors, not relaxed. Upstream question:
    https://github.com/adcontextprotocol/adcp/issues/6879
    """
    from google.protobuf import json_format

    from src.a2a_server.adcp_a2a_server import _dict_to_value

    payload = {
        "supported_majors": [3, 4],
        "context": {"retry_after": 30, "nested": {"attempt": 1}, "ids": [7, 8]},
        "dry_run": True,
        "live": False,
    }

    on_the_wire = json_format.MessageToDict(_dict_to_value(payload))

    # Integers are retyped to float at every depth: top level via a list,
    # dict-nested, doubly-nested, and list-nested inside a dict.
    assert on_the_wire["supported_majors"] == [3.0, 4.0]
    assert [type(v) for v in on_the_wire["supported_majors"]] == [float, float]
    assert type(on_the_wire["context"]["retry_after"]) is float
    assert type(on_the_wire["context"]["nested"]["attempt"]) is float
    assert [type(v) for v in on_the_wire["context"]["ids"]] == [float, float]

    # bool is NOT collapsed into the numeric field — it keeps its own type.
    assert on_the_wire["dry_run"] is True
    assert on_the_wire["live"] is False

    # The loss is visible in the serialized bytes buyers actually receive,
    # not merely in the decoded Python objects.
    wire_json = "".join(json_format.MessageToJson(_dict_to_value(payload)).split())
    assert '"supported_majors":[3.0,4.0]' in wire_json
    assert '"retry_after":30.0' in wire_json
    assert '"dry_run":true' in wire_json
