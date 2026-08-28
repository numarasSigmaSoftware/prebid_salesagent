"""Real-wire presence semantics for update_media_buy.revision."""

import json
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest
from fastmcp import Client
from starlette.testclient import TestClient

from src.app import app
from src.core.main import mcp
from src.core.schemas import UpdateMediaBuyRequest, UpdateMediaBuyResult, UpdateMediaBuySuccess
from tests.factories.principal import PrincipalFactory
from tests.helpers import assert_envelope_shape

_IDENTITY = PrincipalFactory.make_identity(
    principal_id="revision-boundary-principal",
    tenant_id="revision-boundary-tenant",
    tenant={"tenant_id": "revision-boundary-tenant"},
    protocol="mcp",
)
_VALID_REQUEST = {
    "media_buy_id": "mb-revision-boundary",
    "paused": True,
    "idempotency_key": "revision-boundary-key-0001",
}


def _success_result() -> UpdateMediaBuyResult:
    return UpdateMediaBuyResult(
        response=UpdateMediaBuySuccess.carrier(media_buy_id="mb-revision-boundary", affected_packages=[]),
        status="completed",
    )


@dataclass
class _BoundaryHarness:
    impl: MagicMock
    rest_identity: object


@pytest.fixture
def boundary(mocker) -> _BoundaryHarness:
    rest_identity = _IDENTITY.model_copy(update={"protocol": "rest"})
    mocker.patch("src.core.mcp_auth_middleware.resolve_identity_from_context", return_value=_IDENTITY)
    mocker.patch("src.core.resolved_identity.resolve_identity", return_value=rest_identity)
    impl = mocker.patch("src.core.tools.media_buy_update._update_media_buy_impl")
    return _BoundaryHarness(impl=impl, rest_identity=rest_identity)


@pytest.mark.asyncio
async def test_mcp_omitted_revision_reaches_impl(boundary: _BoundaryHarness) -> None:
    """The real MCP TypeAdapter must preserve omission as the accepted path."""
    boundary.impl.return_value = _success_result()
    async with Client(mcp) as client:
        result = await client.call_tool("update_media_buy", _VALID_REQUEST, raise_on_error=False)

    assert not result.is_error, result.content
    assert result.structured_content["media_buy_id"] == "mb-revision-boundary"
    boundary.impl.assert_called_once_with(
        req=UpdateMediaBuyRequest(**_VALID_REQUEST),
        identity=_IDENTITY,
        context_id=None,
        raw_wire_payload=_VALID_REQUEST,
    )


@pytest.mark.asyncio
async def test_mcp_valid_revision_reaches_impl(boundary: _BoundaryHarness) -> None:
    """A schema-valid revision reaches the atomic implementation."""
    boundary.impl.return_value = _success_result()
    async with Client(mcp) as client:
        result = await client.call_tool(
            "update_media_buy",
            {**_VALID_REQUEST, "revision": 5},
            raise_on_error=False,
        )

    assert not result.is_error, result.content
    boundary.impl.assert_called_once_with(
        req=UpdateMediaBuyRequest(**{**_VALID_REQUEST, "revision": 5}),
        identity=_IDENTITY,
        context_id=None,
        raw_wire_payload={**_VALID_REQUEST, "revision": 5},
    )


@pytest.mark.asyncio
async def test_mcp_explicit_null_revision_is_invalid_request(boundary: _BoundaryHarness) -> None:
    """An explicit JSON null is schema-invalid, not a spelling of omission.

    ``update-media-buy-request.json`` (v3.1.1) types ``revision`` as
    ``{type: integer, minimum: 1}``, which null violates — and no conformant
    client emits it: at the pinned adcp 6.6.0 an unset ``revision`` is OMITTED
    from ``UpdateMediaBuyRequest.model_dump()``, not serialized as null. It
    therefore lands on INVALID_REQUEST with 0 / "7" / 7.5.
    """
    async with Client(mcp) as client:
        result = await client.call_tool(
            "update_media_buy",
            {**_VALID_REQUEST, "revision": None},
            raise_on_error=False,
        )

    assert result.is_error
    assert_envelope_shape(
        json.loads(result.content[0].text),
        "INVALID_REQUEST",
        recovery="correctable",
        message_substr="must be an integer",
    )
    boundary.impl.assert_not_called()


def test_rest_omitted_revision_reaches_impl(boundary: _BoundaryHarness) -> None:
    """The real REST body model must preserve omission as the accepted path."""
    boundary.impl.return_value = _success_result()
    client = TestClient(app)
    try:
        response = client.put(
            "/api/v1/media-buys/mb-revision-boundary",
            json={key: value for key, value in _VALID_REQUEST.items() if key != "media_buy_id"},
            headers={"Authorization": "Bearer revision-boundary-token"},
        )
    finally:
        client.close()

    assert response.status_code == 200, response.text
    assert response.json()["media_buy_id"] == "mb-revision-boundary"
    boundary.impl.assert_called_once_with(
        req=UpdateMediaBuyRequest(**_VALID_REQUEST),
        identity=boundary.rest_identity,
        context_id=None,
        raw_wire_payload={
            "paused": True,
            "idempotency_key": "revision-boundary-key-0001",
        },
    )


def test_rest_explicit_null_revision_is_invalid_request(boundary: _BoundaryHarness) -> None:
    """REST parity: an explicit null is the schema violation MCP rejects too."""
    body = {key: value for key, value in _VALID_REQUEST.items() if key != "media_buy_id"}
    body["revision"] = None
    client = TestClient(app, raise_server_exceptions=False)
    try:
        response = client.put(
            "/api/v1/media-buys/mb-revision-boundary",
            json=body,
            headers={"Authorization": "Bearer revision-boundary-token"},
        )
    finally:
        client.close()

    assert response.status_code == 400, response.text
    assert_envelope_shape(
        response.json(),
        "INVALID_REQUEST",
        recovery="correctable",
        message_substr="must be an integer",
    )
    boundary.impl.assert_not_called()


def test_rest_valid_revision_reaches_impl(boundary: _BoundaryHarness) -> None:
    """REST forwards a valid optimistic-concurrency precondition."""
    boundary.impl.return_value = _success_result()
    body = {key: value for key, value in _VALID_REQUEST.items() if key != "media_buy_id"}
    body["revision"] = 5
    client = TestClient(app, raise_server_exceptions=False)
    try:
        response = client.put(
            "/api/v1/media-buys/mb-revision-boundary",
            json=body,
            headers={"Authorization": "Bearer revision-boundary-token"},
        )
    finally:
        client.close()

    assert response.status_code == 200, response.text
    boundary.impl.assert_called_once_with(
        req=UpdateMediaBuyRequest(**{**_VALID_REQUEST, "revision": 5}),
        identity=boundary.rest_identity,
        context_id=None,
        raw_wire_payload={
            "paused": True,
            "idempotency_key": "revision-boundary-key-0001",
            "revision": 5,
        },
    )


def test_rest_below_minimum_revision_is_invalid_request(boundary: _BoundaryHarness) -> None:
    """revision 0 is schema-invalid (minimum 1) — INVALID_REQUEST, matching BR-UC-003 below_min."""
    body = {key: value for key, value in _VALID_REQUEST.items() if key != "media_buy_id"}
    body["revision"] = 0
    client = TestClient(app, raise_server_exceptions=False)
    try:
        response = client.put(
            "/api/v1/media-buys/mb-revision-boundary",
            json=body,
            headers={"Authorization": "Bearer revision-boundary-token"},
        )
    finally:
        client.close()

    assert response.status_code == 400, response.text
    assert_envelope_shape(
        response.json(),
        "INVALID_REQUEST",
        recovery="correctable",
        message_substr="must be an integer",
    )
    boundary.impl.assert_not_called()
