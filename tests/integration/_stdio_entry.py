"""Entry point spawned by tests/integration/test_stdio_roundtrip.py as a real OS
subprocess over stdio (via the mcp client SDK's StdioServerParameters). Runs the REAL
FastMCP server (`sfdc_mcp.server`) with the REAL `SfdcClient` pointed at the fake
Salesforce REST API the test starts in-process — only auth is stubbed, since real
Salesforce device-code/login flows are unreachable from a test.

The fake API's port is passed in via the SFDC_MCP_TEST_PORT env var (set on the
subprocess's environment by the test) rather than hardcoded, since the test binds an
OS-assigned free port.

`Settings()` is still constructed here — with `_env_file=None` — purely to source
api_version/item_limit/timeout_seconds the same way the real console-script entry
point (`sfdc_mcp.server.main`) does; `_env_file=None` means a stray `.env` file
sitting in this checkout's repo root can never bleed into the gate, on top of the
test's own env-scrub of SFDC_MCP_* when it spawns this subprocess (the
snow-mcp-server af69aa0 lesson) — belt and braces. `SFDC_MCP_CLIENT_ID` is set to a
throwaway value purely to satisfy Settings' device_code validation matrix; the real
DeviceCodeAuth this would normally build is never constructed — `_StubAuth` stands in
its place below, so the interactive device-code flow is never exercised here.
"""

import os

from sfdc_mcp import server
from sfdc_mcp.client import SfdcClient
from sfdc_mcp.settings import Settings


class _StubAuth:
    """Always has a token ready and a fixed instance_url pointed at the fake
    Salesforce REST API this test starts on 127.0.0.1 — no LoginRequired, no
    interactive device-code flow."""

    def __init__(self, instance_url: str):
        self._instance_url = instance_url
        # Real AuthProvider contract: instance_url() raises until get_token() has
        # succeeded at least once — see AuthProvider.instance_url's docstring.
        self._has_succeeded = False

    def get_token(self, force_refresh: bool = False) -> str:
        self._has_succeeded = True
        return "integration-test-token"

    def instance_url(self) -> str:
        if not self._has_succeeded:
            raise RuntimeError(
                "instance_url() was called before any get_token() succeeded — sign "
                "in first."
            )
        return self._instance_url


def main() -> None:
    port = os.environ["SFDC_MCP_TEST_PORT"]
    settings = Settings(
        _env_file=None,
        client_id="gate-client-id",
        login_url="https://login.salesforce.com",
    )
    auth = _StubAuth(f"http://127.0.0.1:{port}")
    client = SfdcClient(
        auth,
        api_version=settings.api_version,
        item_limit=settings.item_limit,
        timeout_seconds=settings.timeout_seconds,
    )
    server.configure(client, auth, settings.item_limit)
    server.mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
