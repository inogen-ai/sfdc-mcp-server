import threading
import time

import httpx
import pytest

from sfdc_mcp import auth as auth_module
from sfdc_mcp import client as client_module
from sfdc_mcp import server
from sfdc_mcp.auth import AuthError, ClientCredentialsAuth, DeviceCodeAuth, LoginRequired
from sfdc_mcp.client import SfdcClient

INSTANCE_URL = "https://acme.my.salesforce.com"
API_VERSION = "62.0"

# 15- and 18-char record ids (base-62 shape only matters — these aren't real ids).
ID_15 = "001" + "x" * 12
ID_18 = "001" + "x" * 15


class FakeAuth:
    """Stands in for an AuthProvider that always has a token ready — no
    LoginRequired, no interactive flow."""

    def __init__(self, token="token-1", instance_url=INSTANCE_URL):
        self._token = token
        self._instance_url = instance_url
        # Real AuthProvider contract: instance_url() raises until get_token() has
        # succeeded at least once — see AuthProvider.instance_url's docstring.
        self._has_succeeded = False

    def get_token(self, force_refresh: bool = False) -> str:
        self._has_succeeded = True
        return self._token

    def instance_url(self) -> str:
        if not self._has_succeeded:
            raise RuntimeError(
                "instance_url() was called before any get_token() succeeded — sign "
                "in first."
            )
        return self._instance_url


class LoginRequiredAuth:
    """Raises LoginRequired on every get_token() call, like DeviceCodeAuth before the
    device flow completes. `with_complete_login=False` mimics an AuthProvider with no
    completion story at all, so server.py's completion-thread guard can be proven not
    to crash on it."""

    def __init__(
        self,
        message="To sign in, open https://x/setup and enter the code ABC-123.",
        with_complete_login=True,
    ):
        self.message = message
        self.complete_calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        if with_complete_login:
            self.complete_login = self._complete_login

    def get_token(self, force_refresh: bool = False) -> str:
        raise LoginRequired(self.message)

    def instance_url(self) -> str:
        # Real AuthProvider contract: get_token() never succeeds on this fake, so
        # instance_url() must never return a value either — see
        # AuthProvider.instance_url's docstring.
        raise RuntimeError(
            "instance_url() was called before any get_token() succeeded — sign in "
            "first."
        )

    def _complete_login(self) -> str:
        self.complete_calls += 1
        self.started.set()
        self.release.wait(timeout=2)
        return "device-flow-token"


class AuthErrorAuth:
    def get_token(self, force_refresh: bool = False) -> str:
        raise AuthError(
            "Salesforce rejected the client credentials — check SFDC_MCP_CLIENT_ID "
            "and SFDC_MCP_CLIENT_SECRET."
        )

    def instance_url(self) -> str:
        # Real AuthProvider contract: get_token() never succeeds on this fake, so
        # instance_url() must never return a value either — see
        # AuthProvider.instance_url's docstring.
        raise RuntimeError(
            "instance_url() was called before any get_token() succeeded — sign in "
            "first."
        )


def _client(handler, auth=None) -> SfdcClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return SfdcClient(auth or FakeAuth(), API_VERSION, http=http)


@pytest.fixture(autouse=True)
def _fake_sleep(monkeypatch):
    """None of these tests exercise the retry loop on purpose — patch client.sleep for
    every test in this module so a stray 429/503 in a handler can't block for real."""
    monkeypatch.setattr(client_module, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _reset_server_state():
    """server._state and the completion-thread singleton are module globals — reset
    them around every test so one test's configure()/spawned thread can't leak into
    the next."""
    server._state = None
    server._completion_thread = None
    yield
    server._state = None
    server._completion_thread = None


# -- SELECT-only guard -----------------------------------------------------------------


def _no_call_client() -> SfdcClient:
    def handler(request):
        raise AssertionError("SELECT guard must reject before any network call")

    return _client(handler)


REJECTED_QUERIES = [
    "UPDATE Account SET Name='x'",
    "DELETE FROM Account",
    "INSERT INTO Account (Name) VALUES ('x')",
    "// only a comment, no query at all",
    "// leading line comment\nUPDATE Account SET Name='x'",
    "/* leading block comment */ DELETE FROM Account",
    "",
    "   ",
    "/* unterminated comment with no closer",
]


@pytest.mark.parametrize("query", REJECTED_QUERIES)
def test_soql_query_rejects_non_select(query):
    server.configure(_no_call_client(), FakeAuth())

    result = server._soql_query_sync(query)

    assert "read-only" in result
    assert "SELECT" in result


def test_soql_query_accepts_leading_whitespace():
    def handler(request):
        assert request.url.params["q"] == "  SELECT Id FROM Account"
        return httpx.Response(
            200, json={"totalSize": 1, "done": True, "records": [{"Id": "001x"}]}
        )

    server.configure(_client(handler), FakeAuth())

    result = server._soql_query_sync("  SELECT Id FROM Account")

    assert "Id: 001x" in result


def test_soql_query_accepts_leading_comments_and_lowercase_select():
    def handler(request):
        return httpx.Response(
            200, json={"totalSize": 1, "done": True, "records": [{"Id": "001x"}]}
        )

    server.configure(_client(handler), FakeAuth())

    result = server._soql_query_sync("/* note */ // also\nselect Id from Account")

    assert "Id: 001x" in result


# -- soql_query happy path / record formatting / count note / usage note --------------


def test_soql_query_happy_path_orders_id_name_then_rest_skips_attributes():
    captured = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "totalSize": 1,
                "done": True,
                "records": [
                    {
                        "attributes": {"type": "Account", "url": "/x"},
                        "Industry": "Tech",
                        "Id": "001xx0000006abcAAA",
                        "Name": "Acme",
                    }
                ],
            },
        )

    server.configure(_client(handler), FakeAuth())

    result = server._soql_query_sync("SELECT Id, Name, Industry FROM Account", limit=5)

    assert captured["params"]["q"] == "SELECT Id, Name, Industry FROM Account"
    lines = result.splitlines()
    assert lines[0] == "Id: 001xx0000006abcAAA"
    assert lines[1] == "Name: Acme"
    assert "Industry: Tech" in result
    assert "attributes" not in result


def test_soql_query_renders_relationship_field_as_dotted_path_one_level_deep():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "totalSize": 1,
                "done": True,
                "records": [
                    {
                        "Id": "006xx000000abcAAA",
                        "Name": "Widget",
                        "Account": {
                            "attributes": {"type": "Account", "url": "/x"},
                            "Name": "Acme",
                            "Industry": "Tech",
                        },
                    }
                ],
            },
        )

    server.configure(_client(handler), FakeAuth())

    result = server._soql_query_sync("SELECT Id, Name, Account.Name FROM Opportunity")

    assert "Account.Name: Acme" in result
    assert "Account.Industry: Tech" in result
    assert "Account.attributes" not in result


def test_soql_query_no_records_returns_friendly_message():
    def handler(request):
        return httpx.Response(200, json={"totalSize": 0, "done": True, "records": []})

    server.configure(_client(handler), FakeAuth())

    result = server._soql_query_sync("SELECT Id FROM Account WHERE Name = 'nope'")

    assert "No records matched" in result


def test_soql_query_count_note_when_total_exceeds_shown():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "totalSize": 50,
                "done": True,
                "records": [{"Id": str(i)} for i in range(3)],
            },
        )

    server.configure(_client(handler), FakeAuth())

    result = server._soql_query_sync("SELECT Id FROM Account", limit=3)

    assert "showing 3 of 50 reported matches" in result
    assert "more available" not in result


def test_soql_query_no_count_note_when_total_equals_shown():
    def handler(request):
        return httpx.Response(
            200,
            json={"totalSize": 2, "done": True, "records": [{"Id": "1"}, {"Id": "2"}]},
        )

    server.configure(_client(handler), FakeAuth())

    result = server._soql_query_sync("SELECT Id FROM Account")

    assert "showing" not in result


def test_soql_query_appends_usage_note_at_ninety_percent():
    def handler(request):
        return httpx.Response(
            200,
            headers={"Sforce-Limit-Info": "api-usage=13500/15000"},
            json={"totalSize": 1, "done": True, "records": [{"Id": "1"}]},
        )

    server.configure(_client(handler), FakeAuth())

    result = server._soql_query_sync("SELECT Id FROM Account")

    assert "13500/15000" in result


def test_soql_query_no_usage_note_below_ninety_percent():
    def handler(request):
        return httpx.Response(
            200,
            headers={"Sforce-Limit-Info": "api-usage=100/15000"},
            json={"totalSize": 1, "done": True, "records": [{"Id": "1"}]},
        )

    server.configure(_client(handler), FakeAuth())

    result = server._soql_query_sync("SELECT Id FROM Account")

    assert "API usage" not in result


# -- get_record: Id shape validation, happy path, fields param ------------------------


BAD_ID_SHAPES = [
    "001" + "x" * 11,  # 14 chars
    "001" + "x" * 13,  # 16 chars
    "001" + "x" * 16,  # 19 chars
    "001-x" + "x" * 10,  # hyphen, not alnum
    "",
]


@pytest.mark.parametrize("bad_id", BAD_ID_SHAPES)
def test_get_record_rejects_bad_id_shape_before_any_network_call(bad_id):
    def handler(request):
        raise AssertionError("bad id shape must be rejected before any network call")

    server.configure(_client(handler), FakeAuth())

    result = server._get_record_sync("Account", bad_id)

    assert "doesn't look like a Salesforce record Id" in result


@pytest.mark.parametrize("good_id", [ID_15, ID_18])
def test_get_record_accepts_15_and_18_char_ids(good_id):
    def handler(request):
        return httpx.Response(200, json={"Id": good_id, "Name": "Acme"})

    server.configure(_client(handler), FakeAuth())

    result = server._get_record_sync("Account", good_id)

    assert f"Id: {good_id}" in result


def test_get_record_happy_path_with_fields_param():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(
            200, json={"Id": ID_18, "Name": "Acme", "Industry": "Tech"}
        )

    server.configure(_client(handler), FakeAuth())

    result = server._get_record_sync("Account", ID_18, fields="Id,Name,Industry")

    assert f"/sobjects/Account/{ID_18}" in captured["url"]
    assert "fields=" in captured["url"]
    lines = result.splitlines()
    assert lines[0] == f"Id: {ID_18}"
    assert lines[1] == "Name: Acme"
    assert "Industry: Tech" in result


def test_get_record_without_fields_omits_fields_param():
    captured = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"Id": ID_18})

    server.configure(_client(handler), FakeAuth())

    server._get_record_sync("Account", ID_18)

    assert "fields" not in captured["params"]


# -- search: parameterizedSearch GET, sobject scoping, type label, limit --------------


def test_search_happy_path_scopes_sobjects_and_labels_type():
    captured = {}

    def handler(request):
        captured["params"] = list(request.url.params.multi_items())
        return httpx.Response(
            200,
            json={
                "searchRecords": [
                    {
                        "attributes": {"type": "Account", "url": "/x"},
                        "Id": ID_18,
                        "Name": "Acme",
                    }
                ]
            },
        )

    server.configure(_client(handler), FakeAuth())

    result = server._search_sync("acme", sobjects="Account, Contact", limit=10)

    assert ("q", "acme") in captured["params"]
    assert ("sobject", "Account") in captured["params"]
    assert ("sobject", "Contact") in captured["params"]
    assert "[Account]" in result
    assert "Name: Acme" in result


def test_search_unscoped_omits_sobject_param():
    captured = {}

    def handler(request):
        captured["params"] = list(request.url.params.multi_items())
        return httpx.Response(200, json={"searchRecords": []})

    server.configure(_client(handler), FakeAuth())

    server._search_sync("nothing")

    assert all(key != "sobject" for key, _value in captured["params"])


def test_search_no_results_returns_friendly_message():
    def handler(request):
        return httpx.Response(200, json={"searchRecords": []})

    server.configure(_client(handler), FakeAuth())

    result = server._search_sync("zzz")

    assert "No results" in result


def test_search_respects_limit_client_side():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "searchRecords": [
                    {"attributes": {"type": "Contact"}, "Id": str(i)} for i in range(5)
                ]
            },
        )

    server.configure(_client(handler), FakeAuth())

    result = server._search_sync("x", limit=2)

    assert result.count("[Contact]") == 2


# -- describe_sobject: field listing, picklist cap, field cap -------------------------


def test_describe_sobject_happy_path_lists_fields_and_caps_picklist_values():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "label": "Account",
                "fields": [
                    {"name": "Id", "type": "id", "label": "Record ID"},
                    {
                        "name": "Industry",
                        "type": "picklist",
                        "label": "Industry",
                        "picklistValues": [
                            {"value": f"V{i}", "active": True} for i in range(12)
                        ],
                    },
                ],
            },
        )

    server.configure(_client(handler), FakeAuth())

    result = server._describe_sobject_sync("Account")

    assert "Id (id, Record ID)" in result
    assert "Industry (picklist, Industry)" in result
    assert "V9" in result
    assert "V10" not in result
    assert "…" in result


def test_describe_sobject_caps_fields_at_200():
    fields = [
        {"name": f"Field{i}", "type": "string", "label": f"Field {i}"} for i in range(250)
    ]

    def handler(request):
        return httpx.Response(200, json={"label": "Big", "fields": fields})

    server.configure(_client(handler), FakeAuth())

    result = server._describe_sobject_sync("Big")

    assert "Field199" in result
    assert "Field200 (" not in result
    assert "capped at 200" in result


# -- list_sobjects: queryable filter, cap at item_limit*10 -----------------------------


def test_list_sobjects_filters_non_queryable():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "sobjects": [
                    {"name": "Account", "label": "Account", "queryable": True},
                    {"name": "HiddenThing", "label": "Hidden", "queryable": False},
                ]
            },
        )

    server.configure(_client(handler), FakeAuth())

    result = server._list_sobjects_sync()

    assert "Account — Account" in result
    assert "Hidden" not in result


def test_list_sobjects_caps_at_item_limit_times_ten():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "sobjects": [
                    {"name": f"Obj{i}", "label": f"Obj {i}", "queryable": True}
                    for i in range(25)
                ]
            },
        )

    server.configure(_client(handler), FakeAuth(), item_limit=2)

    result = server._list_sobjects_sync()

    shown_lines = [line for line in result.splitlines() if line.startswith("Obj")]
    assert len(shown_lines) == 20
    assert "more queryable objects not shown" in result


# -- LoginRequired: instructions + background completion spawn ------------------------


def test_login_required_returns_instructions_and_spawns_completion():
    auth = LoginRequiredAuth()
    server.configure(_client(lambda request: httpx.Response(200, json={}), auth=auth), auth)

    result = server._list_sobjects_sync()

    assert "ABC-123" in result
    assert auth.started.wait(timeout=2)
    auth.release.set()


def test_login_required_without_complete_login_does_not_crash():
    auth = LoginRequiredAuth(with_complete_login=False)
    server.configure(_client(lambda request: httpx.Response(200, json={}), auth=auth), auth)

    result = server._get_record_sync("Account", ID_18)

    assert "ABC-123" in result
    assert auth.complete_calls == 0
    time.sleep(0.05)
    assert server._completion_thread is None


def test_login_required_spawns_completion_thread_only_once():
    auth = LoginRequiredAuth()
    server.configure(_client(lambda request: httpx.Response(200, json={}), auth=auth), auth)

    first = server._describe_sobject_sync("Account")
    assert auth.started.wait(timeout=2)

    second = server._list_sobjects_sync()  # hits LoginRequired again while thread is alive

    assert "ABC-123" in first
    assert "ABC-123" in second
    auth.release.set()
    time.sleep(0.05)
    assert auth.complete_calls == 1  # only ever spawned once


def test_soql_query_login_required_returns_instructions_without_any_query():
    auth = LoginRequiredAuth(with_complete_login=False)
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(200, json={})

    server.configure(_client(handler, auth=auth), auth)

    result = server._soql_query_sync("SELECT Id FROM Account")

    assert "ABC-123" in result
    assert called["n"] == 0  # get_token() raised before any HTTP request was made


# -- regression: get_token() before instance_url() ordering (critical fix) -----------
#
# Before the fix, `SfdcClient._request` resolved the URL (calling
# `auth.instance_url()`) before calling `auth.get_token()`. Both DeviceCodeAuth and
# ClientCredentialsAuth only populate `instance_url()` as a side effect of a
# successful token response, and raise RuntimeError from `instance_url()` until that
# has happened — so the very first request in EITHER auth mode raised a raw
# RuntimeError that escaped every tool's `except (LoginRequired, AuthError,
# SfdcError)` clause, rather than the intended LoginRequired sign-in instructions
# (device_code) or a working request (client_credentials, which never gets a separate
# "first login" moment — every call is a fresh-or-cached client-credentials fetch).
# These use the REAL auth providers (not the module's fakes) over MockTransport, so a
# regression in the ordering fails loudly here rather than being masked by a fake that
# doesn't implement the real contract.


def test_login_required_first_call_with_real_device_code_auth_returns_instructions(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(auth_module, "sleep", lambda seconds: None)

    def auth_handler(request):
        return httpx.Response(
            200,
            json={
                "device_code": "long-device-code",
                "user_code": "X1D9SEET",
                "verification_uri": "https://acme.my.salesforce.com/setup/connect",
                "interval": 5,
            },
        )

    auth = DeviceCodeAuth(
        client_id="client-id",
        login_url="https://login.salesforce.com",
        token_cache_path=str(tmp_path / "cache.json"),
        http=httpx.Client(transport=httpx.MockTransport(auth_handler)),
    )

    def rest_handler(request):
        raise AssertionError("no REST call should be made before sign-in completes")

    server.configure(_client(rest_handler, auth=auth), auth)

    result = server._list_sobjects_sync()

    assert "X1D9SEET" in result
    assert "https://acme.my.salesforce.com/setup/connect" in result


def test_client_credentials_first_call_succeeds_end_to_end_with_real_auth():
    calls = {"n": 0}

    def combined_handler(request):
        calls["n"] += 1
        if request.url.path.endswith("/services/oauth2/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "cc-token",
                    "instance_url": INSTANCE_URL,
                    "token_type": "Bearer",
                },
            )
        assert request.headers["authorization"] == "Bearer cc-token"
        return httpx.Response(
            200,
            json={"sobjects": [{"name": "Account", "label": "Account", "queryable": True}]},
        )

    shared_http = httpx.Client(transport=httpx.MockTransport(combined_handler))
    auth = ClientCredentialsAuth(
        client_id="client-id",
        client_secret="client-secret",
        login_url="https://login.salesforce.com",
        http=shared_http,
    )
    client = SfdcClient(auth, API_VERSION, http=shared_http)
    server.configure(client, auth)

    result = server._list_sobjects_sync()

    assert "Account — Account" in result
    assert calls["n"] == 2


# -- AuthError / SfdcError passthrough -------------------------------------------------


def test_auth_error_returns_message_not_traceback():
    auth = AuthErrorAuth()
    server.configure(_client(lambda request: httpx.Response(200, json={}), auth=auth), auth)

    result = server._list_sobjects_sync()

    assert "Traceback" not in result
    assert "rejected the client credentials" in result


def test_sfdc_error_returns_message_not_traceback():
    def handler(request):
        return httpx.Response(404, json=[{"message": "Not Found", "errorCode": "NOT_FOUND"}])

    server.configure(_client(handler), FakeAuth())

    result = server._describe_sobject_sync("Frobnicator")

    assert "Traceback" not in result
    assert "Frobnicator" in result


# -- main() --------------------------------------------------------------------------


def test_main_bad_auth_config_exits_cleanly_with_actionable_sentence(monkeypatch, capsys):
    """Settings()'s ValueError (bad/missing config) must not surface as a traceback
    from main() — a clean stderr sentence and exit(1) instead."""
    monkeypatch.delenv("SFDC_MCP_CLIENT_ID", raising=False)
    monkeypatch.setenv("SFDC_MCP_AUTH", "bogus")

    with pytest.raises(SystemExit) as exc_info:
        server.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "SFDC_MCP_AUTH" in captured.err
    assert "Traceback" not in captured.err


# -- async offload ---------------------------------------------------------------------


@pytest.fixture
def anyio_backend():
    # Only asyncio is a dependency here (trio isn't installed) — pin the anyio pytest
    # plugin's backend parametrization to the one we actually run under.
    return "asyncio"


@pytest.mark.anyio
async def test_soql_query_tool_wrapper_offloads_to_worker_thread():
    """The registered `soql_query` tool is `async def` and must actually await its
    sync body via anyio.to_thread.run_sync rather than blocking the event loop — call
    the real wrapper (not `_soql_query_sync`) from inside a running loop and confirm
    the sync body ran on a different thread than the test itself."""
    caller_thread = threading.get_ident()
    seen_thread = {}

    def handler(request):
        seen_thread["id"] = threading.get_ident()
        return httpx.Response(
            200, json={"totalSize": 1, "done": True, "records": [{"Id": "1"}]}
        )

    server.configure(_client(handler), FakeAuth())

    result = await server.soql_query("SELECT Id FROM Account")

    assert "Id: 1" in result
    assert seen_thread["id"] != caller_thread
