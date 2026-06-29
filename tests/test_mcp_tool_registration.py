"""MCP tool registration tests — verifies that all 5 tools are declared on the
FastMCP server at import time.

These tests are intentionally DB-free: no autouse DB fixtures, no PostgreSQL
requirement.  The @mcp.tool decorator registers tools when the module loads,
so checking mcp.list_tools() does not need a database connection.
"""

from __future__ import annotations

import pytest


class TestMCPToolRegistration:
    """Verify that all expected tools are registered on the FastMCP server.

    These are import-time tests — the ``@mcp.tool`` decorators run when
    ``mcp_server`` is first imported, so no live services are required.
    """

    EXPECTED_TOOLS = {
        "get_next_city",
        "report_website",
        "get_unaudited_website",
        "submit_audit",
        "get_stats",
    }

    @pytest.mark.asyncio
    async def test_all_five_tools_registered(self) -> None:
        """Importing mcp_server registers all 5 tools on the FastMCP instance."""
        from agency_audit.mcp_server import mcp

        tools = await mcp.list_tools()
        registered = {t.name for t in tools}

        missing = self.EXPECTED_TOOLS - registered
        extra = registered - self.EXPECTED_TOOLS

        assert not missing, f"Missing tools: {missing}"
        assert not extra, f"Unexpected extra tools: {extra}"

    @pytest.mark.asyncio
    async def test_tools_registered_by_name(self) -> None:
        """Each tool function name appears in the registered tool list."""
        from agency_audit.mcp_server import mcp

        tools = await mcp.list_tools()
        registered = {t.name for t in tools}

        for name in self.EXPECTED_TOOLS:
            assert name in registered, f"Tool {name!r} not found in registered tools"
