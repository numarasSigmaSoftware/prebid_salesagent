"""Shared test helpers for A2A handler tests.

Provides make_a2a_context() to build a ServerCallContext the same way
AdCPCallContextBuilder.build() does in production, but without needing
a Starlette request object, and extract_processing_error_envelope() to
read the two-layer AdCP error envelope off a failed Task returned by
on_message_send's outer error handler.
"""

import json
import uuid

from a2a.server.context import ServerCallContext
from a2a.types import Message, Part, Role, SendMessageRequest
from google.protobuf import json_format

from src.core.auth_context import AUTH_CONTEXT_STATE_KEY, AuthContext
from src.core.resolved_identity import ResolvedIdentity
from tests.factories import PrincipalFactory


def make_a2a_context(
    auth_token: str | None = None,
    headers: dict[str, str] | None = None,
) -> ServerCallContext:
    """Build a ServerCallContext for A2A handler tests.

    Mirrors AdCPCallContextBuilder.build() — populates state["auth_context"]
    with an AuthContext containing the given token and headers.

    Args:
        auth_token: Bearer token (None for unauthenticated).
        headers: HTTP headers dict (e.g., {"host": "acme.example.com"}).

    Returns:
        ServerCallContext ready to pass to handler.on_message_send(params, context=ctx).
    """
    auth_ctx = AuthContext(auth_token=auth_token, headers=headers or {})
    return ServerCallContext(state={AUTH_CONTEXT_STATE_KEY: auth_ctx})


def extract_processing_error_envelope(task) -> dict:
    """Read the two-layer AdCP envelope from a failed Task's processing_error artifact.

    ``on_message_send``'s outer error handler attaches the envelope built by
    ``AdCPRequestHandler._build_error_envelope`` to the failed Task as a
    ``processing_error`` artifact carrying the adcp_error DataPart plus a
    recommended human-readable TextPart (AdCP 3.1.1 a2a-response-format.mdx
    "Where the Error Lives": a Task-execution failure rides in the task body).
    Scan for the DataPart — it is not necessarily ``parts[0]`` once a TextPart leads.
    """
    assert task.artifacts, "failed Task must carry the error envelope artifact"
    artifact = task.artifacts[0]
    assert artifact.name == "processing_error", f"expected processing_error artifact, got {artifact.name!r}"
    for part in artifact.parts:
        if part.HasField("data"):
            return json.loads(json_format.MessageToJson(part.data))
    raise AssertionError("processing_error artifact must carry a DataPart")


def make_mock_a2a_identity() -> ResolvedIdentity:
    """Standard mock ResolvedIdentity for A2A handler unit tests."""
    return PrincipalFactory.make_identity(
        principal_id="test-principal",
        tenant_id="test-tenant",
        tenant={"tenant_id": "test-tenant"},
        protocol="a2a",
    )


def make_nl_send_message_request(text: str) -> SendMessageRequest:
    """Build a minimal A2A SendMessageRequest carrying NL text (no skills)."""
    message = Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
    )
    message.parts.append(Part(text=text))
    return SendMessageRequest(message=message)
