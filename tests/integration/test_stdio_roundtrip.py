"""Stdio round-trip integration gate.

Spawns the REAL sfdc-mcp-server process over stdio (via the mcp client SDK's
`StdioServerParameters`/`stdio_client`) with the REAL `SfdcClient` pointed at a fake
Salesforce REST API that this pytest process serves in-process on a background thread
— the fake server binds an OS-assigned free port (never a hardcoded one, and never a
second OS process; see tests/integration/_fake_salesforce.py) and its port is handed
to the spawned server subprocess via the SFDC_MCP_TEST_PORT env var.

Only auth is stubbed (see tests/integration/_stdio_entry.py) since real Salesforce
device-code/login flows are unreachable from a test. Everything else — the FastMCP
stdio transport, the async tool wrappers' worker-thread offload, SfdcClient's
request/pagination/formatting logic, and the SELECT-only guard — is exercised for
real, end-to-end, across all five tools.

The subprocess env is scrubbed of any SFDC_MCP_* a developer's shell or CI runner
happens to export (the snow-mcp-server af69aa0 lesson) before the one SFDC_MCP_* var
this gate owns is added back — so a real client id/secret sitting in the environment
can never leak into (or be silently used by) the fake-backed subprocess.

No credentials and no real network calls are involved, so this runs fast and is safe
to leave enabled by default (the `integration` marker documents scope; it isn't used
to exclude this test from CI).
"""

import os
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import _fake_salesforce
from sfdc_mcp.server import _SOQL_REJECTION

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRY_SCRIPT = Path(__file__).with_name("_stdio_entry.py")

EXPECTED_TOOLS = ["describe_sobject", "get_record", "list_sobjects", "search", "soql_query"]


@pytest.mark.integration
@pytest.mark.anyio
async def test_stdio_roundtrip_drives_all_five_tools():
    fake_server = _fake_salesforce.serve()
    port = fake_server.server_address[1]
    try:
        params = StdioServerParameters(
            command="uv",
            args=["run", "python", str(ENTRY_SCRIPT)],
            cwd=str(REPO_ROOT),
            env={
                **{k: v for k, v in os.environ.items() if not k.startswith("SFDC_MCP_")},
                "SFDC_MCP_TEST_PORT": str(port),
            },
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                assert sorted(t.name for t in tools.tools) == EXPECTED_TOOLS

                account_id = _fake_salesforce.ACCOUNT_ID
                checks = [
                    (
                        "soql_query",
                        {"query": "SELECT Id, Name FROM Account"},
                        [account_id, "Acme Corp"],
                    ),
                    (
                        "get_record",
                        {"sobject": "Account", "record_id": account_id},
                        [account_id, "Acme Corp"],
                    ),
                    ("search", {"term": "Acme"}, [account_id, "Acme Corp", "[Account]"]),
                    (
                        "describe_sobject",
                        {"sobject": "Account"},
                        ["Name (string", "Industry (picklist", "Technology", "Banking"],
                    ),
                    ("list_sobjects", {}, ["Account — Account", "Contact — Contact"]),
                ]
                for name, args, expect in checks:
                    result = await session.call_tool(name, args)
                    text = result.content[0].text
                    for needle in expect:
                        assert needle in text, f"{name}: {needle!r} not in {text[:200]!r}"

                # SELECT-only guard: rejected client-side, before any request reaches
                # the fake server, so a mistyped write attempt never even gets to
                # Salesforce.
                rejected = await session.call_tool(
                    "soql_query", {"query": "UPDATE Account SET Name = 'x'"}
                )
                assert rejected.content[0].text == _SOQL_REJECTION
    finally:
        fake_server.shutdown()
        fake_server.server_close()
