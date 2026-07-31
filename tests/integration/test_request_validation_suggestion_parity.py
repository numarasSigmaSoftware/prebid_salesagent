"""Request-validation suggestion parity (#1417).

Core Invariant: every request-validation rejection, on every transport,
crosses the wire as ONE typed AdCPValidationError produced by the single
shared boundary (``adcp_validation_boundary``), carrying error.json's
TOP-LEVEL ``suggestion`` (AdCP 3.1, pinned ref v3.1-04f59d2d5,
static/schemas/source/core/error.json — "Suggested action to resolve the
error"; graded by the POST-F3 storyboard steps). A path that constructs its
request outside the boundary leaks a raw pydantic ``ValidationError`` and the
buyer-facing envelope carries NO suggestion.

Each test pins that invariant for one production request-construction site:
remove the site's boundary (or its full-request validation) and the envelope
loses its suggestion. The sites covered (transports in parens):

- ``get_media_buy_delivery`` — GetMediaBuyDeliveryRequest (REST)
- ``get_media_buys`` — ``_handle_get_media_buys_skill`` / GetMediaBuysRequest (A2A)
- ``list_accounts`` — ``_handle_list_accounts_skill`` + ``/api/v1/accounts`` / ListAccountsRequest (A2A, REST)
- ``sync_accounts`` — ``_handle_sync_accounts_skill`` + ``/api/v1/accounts/sync`` / SyncAccountsRequest (A2A, REST)
- ``list_authorized_properties`` — ``_handle_list_authorized_properties_skill`` / ListAuthorizedPropertiesRequest (A2A)
- ``list_creative_formats`` — ``_handle_list_creative_formats_skill`` + ``/api/v1/creative-formats`` / ListCreativeFormatsRequest (A2A, REST)
- ``get_products`` — ``/api/v1/products`` / ``create_get_products_request`` ProductFilters (REST)
- ``create_media_buy`` — ``to_reporting_webhook`` object coercion, ``/api/v1/media-buys`` + A2A handler (REST, A2A)

Wire-first per tests/CLAUDE.md § Error Verification Policy:
``TransportResult.assert_wire_error(..., require_suggestion=True)`` reads the
STRICT top-level suggestion (``extract_wire_suggestion``) from the captured
two-layer envelope. Every A2A case drives the REAL wire — ``on_message_send``
→ skill handler — via the harness A2A dispatch, never a ``*_raw`` wrapper
(those have zero production callers, so their green would be false confidence).
"""

import pytest

INVALID_STATUS_FILTER = ["nonexistent_status"]  # rejected by GetMediaBuyDeliveryRequest
INVALID_ASSET_TYPES = ["not_an_asset_type"]  # rejected by ListCreativeFormatsRequest


@pytest.mark.requires_db
class TestValidationSecretScrubTransportParity:
    """Boundary-wrapped Pydantic errors are safe on every buyer wire."""

    @pytest.mark.parametrize("transport", ["a2a", "mcp", "rest"])
    def test_extra_field_value_is_never_echoed(self, integration_db, transport):
        from tests.factories import TenantFactory
        from tests.harness import CreativeFormatsEnv
        from tests.harness.transport import Transport
        from tests.helpers.secret_scrub import SECRET_BEARING_MESSAGE, assert_no_secret_leak

        wire_transport = Transport(transport)
        tenant_id = f"validation_scrub_{transport}"
        with CreativeFormatsEnv(tenant_id=tenant_id, principal_id=f"principal_{transport}") as env:
            TenantFactory(tenant_id=tenant_id)
            result = env.call_via(
                wire_transport,
                **{SECRET_BEARING_MESSAGE: SECRET_BEARING_MESSAGE},
            )

        assert result.is_error, f"Expected {transport} to reject the extra field"
        # REST rejects the unknown top-level body member in FastAPI before the
        # route-level request model; AdCP classifies that structural failure as
        # INVALID_REQUEST. A2A/MCP reach the shared strict request model and
        # classify the value as VALIDATION_ERROR.
        expected_code = "INVALID_REQUEST" if transport == "rest" else "VALIDATION_ERROR"
        result.assert_wire_error(
            expected_code,
            recovery="correctable",
            require_suggestion=True,
        )
        assert_no_secret_leak(result.wire_error_envelope)
        error = result.wire_error_envelope["errors"][0]
        assert error["field"] == "unrecognized_field"
        validation_errors = error["details"]["validation_errors"]
        assert validation_errors == [
            {
                "loc": ["unrecognized_field"],
                "msg": "Extra field is not allowed by the AdCP request schema.",
                "type": "unexpected_keyword_argument" if transport == "mcp" else "extra_forbidden",
            }
        ]

    @pytest.mark.parametrize("transport", ["a2a", "mcp", "rest"])
    def test_mapping_key_is_never_echoed(self, integration_db, transport):
        from src.core.product_conversion import convert_pricing_option_to_adcp
        from tests.harness.media_buy_create import MediaBuyCreateEnv
        from tests.harness.transport import Transport
        from tests.helpers.secret_scrub import assert_no_secret_leak

        identifier_secret = "hunter2"
        with MediaBuyCreateEnv() as env:
            _tenant, _principal, product, pricing = env.setup_media_buy_data()
            pricing_option_id = convert_pricing_option_to_adcp(pricing).pricing_option_id
            result = env.call_via(
                Transport(transport),
                brand={"domain": "acme.example"},
                packages=[
                    {
                        "product_id": product.product_id,
                        "budget": 5000.0,
                        "pricing_option_id": pricing_option_id,
                        "targeting_overlay": {
                            "key_value_pairs": {
                                identifier_secret: {"invalid": "value"},
                            }
                        },
                    }
                ],
                start_time="asap",
                end_time="2099-12-31T23:59:59Z",
                idempotency_key=f"mapping-key-scrub-{transport}",
            )

        assert result.is_error
        result.assert_wire_error("VALIDATION_ERROR", recovery="correctable")
        assert_no_secret_leak(result.wire_error_envelope)
        assert identifier_secret not in str(result.wire_error_envelope)
        assert "nested_field" in result.wire_error_envelope["errors"][0]["field"]

    @pytest.mark.parametrize("transport", ["a2a", "mcp", "rest"])
    def test_typed_validation_message_is_never_trusted(self, integration_db, transport):
        from tests.harness.creative_list import CreativeListEnv
        from tests.harness.transport import Transport
        from tests.helpers.secret_scrub import SECRET_BEARING_MESSAGE, assert_no_secret_leak

        with CreativeListEnv() as env:
            result = env.call_via(
                Transport(transport),
                created_after=SECRET_BEARING_MESSAGE,
            )

        assert result.is_error
        result.assert_wire_error("VALIDATION_ERROR", recovery="correctable")
        assert_no_secret_leak(result.wire_error_envelope)
        assert result.wire_error_envelope["errors"][0]["field"] == "created_after"


@pytest.mark.requires_db
class TestGetMediaBuyDeliveryRestSuggestionParity:
    """REST get_media_buy_delivery request-validation must carry a top-level suggestion."""

    def test_invalid_status_filter_rest_envelope_carries_suggestion(self, integration_db):
        """An invalid ``status_filter`` rejected on the REST wire must produce
        the AdCP two-layer VALIDATION_ERROR envelope WITH a top-level
        ``suggestion`` (error.json @v3.1-04f59d2d5).

        Pins that ``get_media_buy_delivery`` builds ``GetMediaBuyDeliveryRequest``
        inside ``adcp_validation_boundary``; without it the raw ValidationError
        reaches the generic ``ValueError`` handler and the 400 envelope has
        code+recovery but NO suggestion.
        """
        from tests.factories import PrincipalFactory, TenantFactory
        from tests.harness import DeliveryPollEnv
        from tests.harness.transport import Transport

        with DeliveryPollEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            PrincipalFactory(tenant=tenant, principal_id="p1")

            result = env.call_via(Transport.REST, status_filter=INVALID_STATUS_FILTER)

            assert result.is_error, (
                f"Invalid status_filter must be rejected on the REST wire, got success payload: {result.payload!r}"
            )
            assert result.envelope["status_code"] == 400
            result.assert_wire_error(
                "VALIDATION_ERROR",
                recovery="correctable",
                require_suggestion=True,
                message_substr="status_filter",
            )


@pytest.mark.requires_db
class TestGetMediaBuysA2ASuggestionParity:
    """A2A get_media_buys request-validation must carry a top-level suggestion.

    Drives the REAL A2A wire (``on_message_send`` →
    ``_handle_get_media_buys_skill``) via the harness A2A dispatch. The
    previous version of this test drove ``get_media_buys_raw`` — a wrapper
    with ZERO production callers — so its green was false confidence
    (#1417): the real skill handler builds ``GetMediaBuysRequest``
    with no ``adcp_validation_boundary`` and leaks a bare ValidationError
    that ``normalize_to_adcp_error`` flattens into a suggestion-less envelope.
    """

    def test_malformed_media_buy_ids_a2a_envelope_carries_suggestion(self, integration_db):
        """A wrong-typed ``media_buy_ids`` (string instead of array) rejected
        on the A2A wire must produce the AdCP two-layer VALIDATION_ERROR
        envelope WITH a top-level ``suggestion`` (error.json
        @v3.1-04f59d2d5), matching what REST emits for the same input.

        Pins that ``_handle_get_media_buys_skill`` validates
        ``GetMediaBuysRequest`` inside the boundary; a bare
        ``model_validate(params)`` yields an envelope with code+recovery but
        NO suggestion.
        """
        from tests.factories import PrincipalFactory, TenantFactory
        from tests.harness.media_buy_list import MediaBuyListEnv
        from tests.harness.transport import Transport

        with MediaBuyListEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            PrincipalFactory(tenant=tenant, principal_id="p1")

            result = env.call_via(Transport.A2A, media_buy_ids="not-a-list")

            assert result.is_error, (
                f"Malformed media_buy_ids must be rejected on the A2A wire, got success payload: {result.payload!r}"
            )
            result.assert_wire_error(
                "VALIDATION_ERROR",
                recovery="correctable",
                require_suggestion=True,
                message_substr="media_buy_ids",
            )


@pytest.mark.requires_db
class TestListAccountsA2ASuggestionParity:
    """A2A list_accounts request-validation must carry a top-level suggestion."""

    def test_invalid_status_a2a_envelope_carries_suggestion(self, integration_db):
        """An invalid ``status`` rejected on the A2A wire must produce the AdCP
        two-layer VALIDATION_ERROR envelope WITH a top-level ``suggestion`` —
        the same enriched envelope REST emits for the same input
        (``/api/v1/accounts`` wraps construction in ``adcp_validation_boundary``).

        Pins that ``_handle_list_accounts_skill`` constructs
        ``ListAccountsRequest`` inside the boundary; a bare construction lets
        the ValidationError reach ``normalize_to_adcp_error`` and the envelope
        loses its suggestion.
        """
        from tests.harness.account_list import AccountListEnv
        from tests.harness.transport import Transport

        with AccountListEnv() as env:
            env.setup_default_data()

            result = env.call_via(Transport.A2A, status="not_a_status")

            assert result.is_error, (
                f"Invalid status must be rejected on the A2A wire, got success payload: {result.payload!r}"
            )
            result.assert_wire_error(
                "VALIDATION_ERROR",
                recovery="correctable",
                require_suggestion=True,
                message_substr="status",
            )


@pytest.mark.requires_db
class TestSyncAccountsA2ASuggestionParity:
    """A2A sync_accounts request-validation must carry a top-level suggestion."""

    def test_account_missing_brand_a2a_envelope_carries_suggestion(self, integration_db):
        """An account entry missing the required ``brand`` rejected on the A2A
        wire must produce the AdCP two-layer VALIDATION_ERROR envelope WITH a
        top-level ``suggestion`` — parity with ``/api/v1/accounts/sync``.

        Pins that ``_handle_sync_accounts_skill`` constructs
        ``SyncAccountsRequest`` inside the boundary; a bare construction drops
        the suggestion.
        """
        from tests.harness.account_sync import AccountSyncEnv
        from tests.harness.transport import Transport

        with AccountSyncEnv() as env:
            env.setup_default_data()

            result = env.call_via(Transport.A2A, accounts=[{"operator": "no-brand.example"}])

            assert result.is_error, (
                "An account entry missing brand must be rejected on the A2A wire, "
                f"got success payload: {result.payload!r}"
            )
            result.assert_wire_error(
                "VALIDATION_ERROR",
                recovery="correctable",
                require_suggestion=True,
                message_substr="brand",
            )


@pytest.mark.requires_db
class TestListAuthorizedPropertiesA2ASuggestionParity:
    """A2A list_authorized_properties request-validation must carry a top-level suggestion."""

    def test_invalid_context_a2a_envelope_carries_suggestion(self, integration_db):
        """A wrong-typed ``context`` rejected on the A2A wire must produce the
        AdCP two-layer VALIDATION_ERROR envelope WITH a top-level
        ``suggestion`` — parity with ``/api/v1/authorized-properties``.

        Pins that ``_handle_list_authorized_properties_skill`` constructs
        ``ListAuthorizedPropertiesRequest`` inside the boundary; a bare
        construction drops the suggestion. (The fifth bare handler, surfaced by
        the disease scan.)
        """
        from tests.factories import PrincipalFactory, TenantFactory
        from tests.harness.authorized_properties import AuthorizedPropertiesEnv
        from tests.harness.transport import Transport

        with AuthorizedPropertiesEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            PrincipalFactory(tenant=tenant, principal_id="p1")

            result = env.call_via(Transport.A2A, context="not-a-context-object")

            assert result.is_error, (
                f"Wrong-typed context must be rejected on the A2A wire, got success payload: {result.payload!r}"
            )
            result.assert_wire_error(
                "VALIDATION_ERROR",
                recovery="correctable",
                require_suggestion=True,
                message_substr="context",
            )


@pytest.mark.requires_db
class TestListCreativeFormatsA2ASuggestionParity:
    """A2A list_creative_formats request-validation must carry a top-level suggestion."""

    def test_invalid_asset_types_a2a_envelope_carries_suggestion(self, integration_db):
        """An invalid ``asset_types`` member rejected on the A2A wire must
        produce the AdCP two-layer VALIDATION_ERROR envelope WITH a top-level
        ``suggestion`` — parity with ``/api/v1/creative-formats``.

        Pins that ``_handle_list_creative_formats_skill`` validates the complete
        ``ListCreativeFormatsRequest`` inside ``adcp_validation_boundary``; a
        bare model-validation call drops the canonical suggestion.
        """
        from tests.harness import CreativeFormatsEnv
        from tests.harness.transport import Transport

        with CreativeFormatsEnv(tenant_id="t1", principal_id="p1") as env:
            result = env.call_via(Transport.A2A, asset_types=INVALID_ASSET_TYPES)

            assert result.is_error, (
                f"Invalid asset_types must be rejected on the A2A wire, got success payload: {result.payload!r}"
            )
            result.assert_wire_error(
                "VALIDATION_ERROR",
                recovery="correctable",
                require_suggestion=True,
                message_substr="asset_types",
            )


@pytest.mark.requires_db
class TestGetProductsRestSuggestionParity:
    """REST get_products request-validation must carry a top-level suggestion."""

    def test_invalid_filters_rest_envelope_carries_suggestion(self, integration_db):
        """An invalid ``filters.delivery_type`` rejected on the REST wire must
        produce the AdCP two-layer VALIDATION_ERROR envelope WITH a top-level
        ``suggestion`` (error.json @v3.1-04f59d2d5).

        The invalid value passes ``GetProductsBody`` (``filters: dict``) and
        fails inside ``ProductFilters`` (delivery_type enum) built by
        ``create_get_products_request`` — the tool-level request-validation
        boundary this invariant governs. The MCP wrapper wraps this helper in
        ``adcp_validation_boundary`` (products.py); the REST route must match.

        Pins that the ``/api/v1/products`` route calls the helper inside the
        boundary; without it the raw ValidationError reaches the generic
        ``ValueError`` handler and the envelope has NO suggestion.
        """
        from tests.harness import ProductEnv
        from tests.harness.transport import extract_wire_suggestion
        from tests.helpers import assert_envelope_shape

        with ProductEnv(tenant_id="t1", principal_id="p1") as env:
            client = env.get_rest_client()

            response = client.post(
                "/api/v1/products",
                json={"brief": "video ads", "filters": {"delivery_type": "not_a_delivery_type"}},
            )

            assert response.status_code == 400, (
                "Invalid filters.delivery_type must be rejected on the REST wire, got "
                f"{response.status_code}: {response.text[:500]}"
            )
            envelope = response.json()
            assert_envelope_shape(
                envelope,
                "VALIDATION_ERROR",
                recovery="correctable",
                message_substr="delivery_type",
            )
            suggestion = extract_wire_suggestion(envelope)
            assert suggestion, (
                "Expected a non-empty TOP-LEVEL suggestion in the VALIDATION_ERROR "
                f"wire envelope (error.json @v3.1-04f59d2d5), got: {envelope}"
            )


@pytest.mark.requires_db
class TestCreateMediaBuyRestWebhookSuggestionParity:
    """REST create_media_buy object-param coercion must carry a top-level suggestion."""

    def test_invalid_reporting_webhook_rest_envelope_carries_suggestion(self, integration_db):
        """A malformed ``reporting_webhook`` rejected on the REST wire must
        produce the AdCP two-layer VALIDATION_ERROR envelope WITH a top-level
        ``suggestion`` (error.json @v3.1-04f59d2d5).

        The invalid value passes ``CreateMediaBuyBody`` (``dict``) and fails
        ``to_reporting_webhook`` coercion (ReportingWebhook requires url /
        authentication / reporting_frequency) — coerced at the route inside
        ``adcp_validation_boundary`` so the rejection is typed instead of the
        raw-ValidationError leak the un-coerced pass-through produced.
        """
        from tests.harness.media_buy_create import MediaBuyCreateEnv
        from tests.harness.transport import extract_wire_suggestion
        from tests.helpers import assert_envelope_shape

        with MediaBuyCreateEnv() as env:
            env.setup_media_buy_data()
            client = env.get_rest_client()

            response = client.post(
                "/api/v1/media-buys",
                json={
                    "brand": {"domain": "acme.example"},
                    "packages": [],
                    "start_time": "asap",
                    "end_time": "2099-12-31T23:59:59Z",
                    "reporting_webhook": {"not_a_webhook_field": True},
                },
            )

            assert response.status_code == 400, (
                "A malformed reporting_webhook must be rejected on the REST wire, got "
                f"{response.status_code}: {response.text[:500]}"
            )
            envelope = response.json()
            assert_envelope_shape(
                envelope,
                "VALIDATION_ERROR",
                recovery="correctable",
            )
            suggestion = extract_wire_suggestion(envelope)
            assert suggestion, (
                "Expected a non-empty TOP-LEVEL suggestion in the VALIDATION_ERROR "
                f"wire envelope (error.json @v3.1-04f59d2d5), got: {envelope}"
            )
            # Suggestion PRESENCE cannot prove the pre-validation ran. AdCPValidationError
            # carries a class-level ``_default_suggestion`` (the canonical enumMetadata
            # text), so every VALIDATION_ERROR has one whether the boundary ran or not —
            # this assertion alone stayed green with the boundary removed. Pin what ONLY
            # full-request pre-validation produces: the schema field path, and the
            # structured message naming each missing sub-field.
            wire_error = envelope["errors"][0]
            assert wire_error.get("field"), (
                f"the boundary attaches the failing schema path; without it the envelope carries no field: {envelope}"
            )
            assert "do not match the AdCP specification" in wire_error["message"], (
                f"expected the boundary's structured multi-field validation message, got {wire_error['message']!r}"
            )
            assert "reporting_frequency" in wire_error["message"], (
                "the message must name the missing sub-fields — the enumeration is what a "
                f"boundary-less coercion cannot produce: {wire_error['message']!r}"
            )


@pytest.mark.requires_db
class TestCreateMediaBuyA2AWebhookSuggestionParity:
    """A2A create_media_buy object-param rejection must carry a top-level suggestion.

    The A2A twin of ``TestCreateMediaBuyRestWebhookSuggestionParity``. The A2A
    skill handler full-request-validates inside ``adcp_validation_boundary``
    BEFORE ``create_media_buy_raw`` runs, which is the only thing standing
    between a malformed ``reporting_webhook`` and the raw wrapper's
    boundary-less ``to_reporting_webhook`` call (#1417: the
    raise-capable ``to_*`` coercions depend on every caller pre-validating).
    This test pins that pre-validation: remove the handler's boundary or its
    full-request validation and the envelope loses its suggestion.
    """

    def test_invalid_reporting_webhook_a2a_envelope_carries_suggestion(self, integration_db):
        """A malformed ``reporting_webhook`` rejected on the A2A wire must
        produce the AdCP two-layer VALIDATION_ERROR envelope WITH a top-level
        ``suggestion`` (error.json @v3.1-04f59d2d5) — identical to REST.
        """
        from tests.harness.media_buy_create import MediaBuyCreateEnv
        from tests.harness.transport import Transport

        with MediaBuyCreateEnv() as env:
            env.setup_media_buy_data()

            result = env.call_via(
                Transport.A2A,
                brand={"domain": "acme.example"},
                packages=[{"product_id": "prod_1", "budget": 5000.0, "pricing_option_id": "cpm_usd_fixed"}],
                start_time="asap",
                end_time="2099-12-31T23:59:59Z",
                reporting_webhook={"not_a_webhook_field": True},
            )

            assert result.is_error, (
                "A malformed reporting_webhook must be rejected on the A2A wire, "
                f"got success payload: {result.payload!r}"
            )
            result.assert_wire_error(
                "VALIDATION_ERROR",
                recovery="correctable",
                require_suggestion=True,
            )
            # Suggestion PRESENCE cannot prove the pre-validation ran. AdCPValidationError
            # carries a class-level ``_default_suggestion`` (the canonical enumMetadata
            # text), so every VALIDATION_ERROR has one whether the boundary ran or not —
            # this assertion alone stayed green with the boundary removed. Pin what ONLY
            # full-request pre-validation produces: the schema field path, and the
            # structured message naming each missing sub-field.
            envelope = result.wire_error_envelope
            wire_error = envelope["errors"][0]
            assert wire_error.get("field"), (
                f"the boundary attaches the failing schema path; without it the envelope carries no field: {envelope}"
            )
            assert "do not match the AdCP specification" in wire_error["message"], (
                f"expected the boundary's structured multi-field validation message, got {wire_error['message']!r}"
            )
            assert "reporting_frequency" in wire_error["message"], (
                "the message must name the missing sub-fields — the enumeration is what a "
                f"boundary-less coercion cannot produce: {wire_error['message']!r}"
            )


@pytest.mark.requires_db
class TestSyncCreativesA2ASuggestionParity:
    """A2A sync_creatives request-validation must carry a top-level suggestion.

    ``_handle_sync_creatives_skill`` constructs ``CreativeAsset(**c)`` from the
    raw wire dict. Bare construction leaks the pydantic ValidationError to
    ``normalize_to_adcp_error`` and the envelope loses its suggestion — the
    exact #1417 disease; this site escaped the sweep because the boundary
    guard matched only ``*Request``-suffixed names (#1417 round-8 review item 3).

    Drives the REAL A2A wire (``on_message_send`` →
    ``_handle_sync_creatives_skill``). ``CreativeSyncEnv.call_a2a`` routes to
    ``sync_creatives_raw`` (zero production callers — pre-existing harness
    debt noted at its definition), so this class overrides it to dispatch
    through the real handler.

    The sibling bare construction, ``ContextObject(**ctx_param)``, has no
    behavioral reproduction: the pinned SDK model declares zero fields with
    ``extra="allow"``, so no dict input can make it raise. It is wrapped in
    the same boundary for guard-consistency (and future SDK field additions).
    """

    def test_invalid_creative_a2a_envelope_carries_suggestion(self, integration_db):
        """A creative entry missing the required ``format_id`` rejected on the
        A2A wire must produce the AdCP two-layer VALIDATION_ERROR envelope WITH
        a top-level ``suggestion`` (error.json @v3.1-04f59d2d5) — parity with
        the nine wrapped skill handlers in the same file.
        """
        from src.core.schemas import SyncCreativesResponse
        from tests.harness.creative_sync import CreativeSyncEnv
        from tests.harness.transport import Transport

        class _RealA2AWireCreativeSyncEnv(CreativeSyncEnv):
            def call_a2a(self, **kwargs):
                return self._run_a2a_handler("sync_creatives", SyncCreativesResponse, **kwargs)

        with _RealA2AWireCreativeSyncEnv() as env:
            env.setup_default_data()

            result = env.call_via(
                Transport.A2A,
                creatives=[{"creative_id": "cr-invalid-1", "name": "No format"}],
            )

            assert result.is_error, (
                "A creative missing format_id must be rejected on the A2A wire, "
                f"got success payload: {result.payload!r}"
            )
            result.assert_wire_error(
                "VALIDATION_ERROR",
                recovery="correctable",
                require_suggestion=True,
                message_substr="format_id",
            )


@pytest.mark.requires_db
class TestListAccountsRestSuggestionParity:
    """REST list_accounts request-validation must carry a top-level suggestion."""

    def test_invalid_status_rest_envelope_carries_suggestion(self, integration_db):
        """An invalid ``status`` rejected on the REST wire must produce the
        AdCP two-layer VALIDATION_ERROR envelope WITH a top-level
        ``suggestion`` (error.json @v3.1-04f59d2d5).

        The invalid value passes ``ListAccountsBody`` (``str``) and fails
        inside ``ListAccountsRequest`` (AccountStatus enum) — the tool-level
        request-validation boundary this invariant governs.

        Pins that the ``/api/v1/accounts`` route builds the request inside the
        boundary; without it the raw ValidationError reaches the generic
        ``ValueError`` handler and the envelope has NO suggestion.
        """
        from tests.harness.account_list import AccountListEnv
        from tests.harness.transport import extract_wire_suggestion
        from tests.helpers import assert_envelope_shape

        with AccountListEnv() as env:
            env.setup_default_data()
            client = env.get_rest_client()

            response = client.post("/api/v1/accounts", json={"status": "not_a_status"})

            assert response.status_code == 400, (
                f"Invalid status must be rejected on the REST wire, got {response.status_code}: {response.text[:500]}"
            )
            envelope = response.json()
            assert_envelope_shape(
                envelope,
                "VALIDATION_ERROR",
                recovery="correctable",
                message_substr="status",
            )
            suggestion = extract_wire_suggestion(envelope)
            assert suggestion, (
                "Expected a non-empty TOP-LEVEL suggestion in the VALIDATION_ERROR "
                f"wire envelope (error.json @v3.1-04f59d2d5), got: {envelope}"
            )


@pytest.mark.requires_db
class TestSyncAccountsRestSuggestionParity:
    """REST sync_accounts request-validation must carry a top-level suggestion."""

    def test_account_missing_brand_rest_envelope_carries_suggestion(self, integration_db):
        """An account entry missing the required ``brand`` rejected on the
        REST wire must produce the AdCP two-layer VALIDATION_ERROR envelope
        WITH a top-level ``suggestion``.

        The invalid entry passes ``SyncAccountsBody`` (``list[dict]``) and
        fails inside ``SyncAccountsRequest`` (Accounts requires brand).

        Pins that the ``/api/v1/accounts/sync`` route builds the request inside
        the boundary; without it the raw ValidationError reaches the generic
        ``ValueError`` handler and the envelope has NO suggestion.
        """
        from tests.harness.account_sync import AccountSyncEnv
        from tests.harness.transport import extract_wire_suggestion
        from tests.helpers import assert_envelope_shape

        with AccountSyncEnv() as env:
            env.setup_default_data()
            client = env.get_rest_client()

            response = client.post(
                "/api/v1/accounts/sync",
                json={"accounts": [{"operator": "no-brand.example"}]},
            )

            assert response.status_code == 400, (
                "An account entry missing brand must be rejected on the REST wire, got "
                f"{response.status_code}: {response.text[:500]}"
            )
            envelope = response.json()
            assert_envelope_shape(
                envelope,
                "VALIDATION_ERROR",
                recovery="correctable",
                message_substr="brand",
            )
            suggestion = extract_wire_suggestion(envelope)
            assert suggestion, (
                "Expected a non-empty TOP-LEVEL suggestion in the VALIDATION_ERROR "
                f"wire envelope (error.json @v3.1-04f59d2d5), got: {envelope}"
            )


@pytest.mark.requires_db
class TestListCreativeFormatsRestSuggestionParity:
    """REST list_creative_formats request-validation must carry a top-level suggestion."""

    def test_invalid_asset_types_rest_envelope_carries_suggestion(self, integration_db):
        """An invalid ``asset_types`` member rejected on the REST wire must
        produce the AdCP two-layer VALIDATION_ERROR envelope WITH a top-level
        ``suggestion``.

        The invalid value passes ``ListCreativeFormatsBody`` (``list[str]``)
        and fails inside ``ListCreativeFormatsRequest`` — the tool-level
        request-validation boundary this invariant governs. Raw-body POST via
        ``get_rest_client`` because the harness ``build_rest_body`` serializes
        a typed request, which cannot represent the invalid input.

        Pins that the route builds the request inside the boundary; without it
        the raw ValidationError reaches the generic ``ValueError`` handler and
        the envelope has NO suggestion.
        """
        from tests.harness import CreativeFormatsEnv
        from tests.harness.transport import extract_wire_suggestion
        from tests.helpers import assert_envelope_shape

        with CreativeFormatsEnv(tenant_id="t1", principal_id="p1") as env:
            client = env.get_rest_client()

            response = client.post(
                "/api/v1/creative-formats",
                json={"asset_types": INVALID_ASSET_TYPES},
            )

            assert response.status_code == 400, (
                "Invalid asset_types must be rejected on the REST wire, got "
                f"{response.status_code}: {response.text[:500]}"
            )
            envelope = response.json()
            assert_envelope_shape(
                envelope,
                "VALIDATION_ERROR",
                recovery="correctable",
                message_substr="asset_types",
            )
            suggestion = extract_wire_suggestion(envelope)
            assert suggestion, (
                "Expected a non-empty TOP-LEVEL suggestion in the VALIDATION_ERROR "
                f"wire envelope (error.json @v3.1-04f59d2d5), got: {envelope}"
            )
