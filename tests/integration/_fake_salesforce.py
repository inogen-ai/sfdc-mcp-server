"""Minimal fake Salesforce REST API for the stdio round-trip integration test
(tests/integration/test_stdio_roundtrip.py): real HTTP, Salesforce REST API response
shapes for just the endpoints the five tools hit. Runs in-process as a daemon thread
bound to an OS-assigned port (port 0) so the test never needs a fixed port or a second
OS process — the same shape as m365-mcp-server's _fake_graph.py and
snow-mcp-server's fake_table_api.py.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# A syntactically valid 18-character Salesforce record Id (base-62 [0-9A-Za-z]) —
# shape-checked by server._valid_record_id before get_record ever calls out.
ACCOUNT_ID = "001000000012345AA1"

_ACCOUNT = {"Id": ACCOUNT_ID, "Name": "Acme Corp", "Industry": "Technology"}

_QUERY_BODY = {
    "totalSize": 1,
    "done": True,
    "records": [{"attributes": {"type": "Account"}, **_ACCOUNT}],
}

_SEARCH_BODY = {
    "searchRecords": [{"attributes": {"type": "Account"}, **_ACCOUNT}],
}

_DESCRIBE_BODY = {
    "label": "Account",
    "fields": [
        {"name": "Id", "type": "id", "label": "Account ID"},
        {"name": "Name", "type": "string", "label": "Account Name"},
        {
            "name": "Industry",
            "type": "picklist",
            "label": "Industry",
            "picklistValues": [
                {"value": "Technology", "active": True},
                {"value": "Banking", "active": True},
            ],
        },
    ],
}

_SOBJECTS_BODY = {
    "sobjects": [
        {"name": "Account", "label": "Account", "queryable": True},
        {"name": "Contact", "label": "Contact", "queryable": True},
        # A non-queryable entry confirms list_sobjects filters on the queryable flag
        # rather than echoing everything Salesforce returns.
        {"name": "HiddenThing__x", "label": "Hidden", "queryable": False},
    ]
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: A003 - stdlib signature
        pass  # keep pytest -q output clean; nothing here is worth logging

    def _json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        # A low, realistic Sforce-Limit-Info reading — well under the 90% usage-note
        # threshold, so the gate's plain string assertions aren't fighting a trailing
        # usage-note line.
        self.send_header("Sforce-Limit-Info", "api-usage=100/15000")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.endswith("/describe"):
            self._json(_DESCRIBE_BODY)
        elif "/parameterizedSearch" in path:
            self._json(_SEARCH_BODY)
        elif path.endswith("/query"):
            self._json(_QUERY_BODY)
        elif path.endswith(f"/sobjects/Account/{ACCOUNT_ID}"):
            self._json(_ACCOUNT)
        elif path.endswith("/sobjects"):
            self._json(_SOBJECTS_BODY)
        else:
            # Salesforce error bodies are JSON arrays of {"message", "errorCode"} —
            # match that shape even for this catch-all so a stray/unexpected request
            # exercises the same error-folding path a real 404 would.
            self._json(
                [{"message": f"No handler for {path!r} in the fake.", "errorCode": "NOT_FOUND"}],
                status=404,
            )


def serve(host: str = "127.0.0.1", port: int = 0) -> HTTPServer:
    """Start the fake Salesforce REST API on a background daemon thread and return the
    bound `HTTPServer` — `port=0` (the default) asks the OS for a free port; read the
    actual port back off `server.server_address[1]`. Caller owns shutdown
    (`server.shutdown()` followed by `server.server_close()`)."""
    server = HTTPServer((host, port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
