"""MCP tolerance for AdCP version-envelope request fields.

AdCP spec 3.1.1 composes core/version-envelope.json (adcp_version,
adcp_major_version) into every request schema via allOf, and every official AdCP
SDK client injects these fields on every request. FastMCP's TypeAdapter
(additionalProperties: false) would reject them before the handler runs unless the
MCP compat middleware strips them first. A2A and REST already tolerate these
fields; this test pins the same tolerance for MCP over the real HTTP transport in
dev mode (the environment where the strip is NOT gated on is_production()).
"""

import pytest
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.requires_db
class TestMCPVersionEnvelopeTolerance:
    """MCP tool calls carrying adcp_version/adcp_major_version succeed."""

    @pytest.fixture
    async def mcp_client(self, mcp_server, sample_tenant, sample_principal, sample_products):
        """Create MCP client for testing with test data."""
        headers = {"x-adcp-auth": sample_principal["access_token"]}
        transport = StreamableHttpTransport(url=f"http://localhost:{mcp_server.port}/mcp/", headers=headers)
        client = Client(transport=transport)

        async with client:
            yield client

    async def test_get_adcp_capabilities_tolerates_version_envelope(self, mcp_client):
        """get_adcp_capabilities with version-envelope fields succeeds and reports versions."""
        result = await mcp_client.call_tool(
            "get_adcp_capabilities",
            {"adcp_version": "3.1", "adcp_major_version": 3},
        )

        assert result is not None
        content = result.structured_content if hasattr(result, "structured_content") else result
        assert content["adcp"]["major_versions"]

    async def test_get_products_tolerates_version_envelope(self, mcp_client):
        """get_products with version-envelope fields succeeds."""
        result = await mcp_client.call_tool(
            "get_products",
            {"brand": {"domain": "testbrand.com"}, "adcp_version": "3.1", "adcp_major_version": 3},
        )

        assert result is not None
        content = result.structured_content if hasattr(result, "structured_content") else result
        assert "products" in content
