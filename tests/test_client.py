import httpx
import pytest

from sfdc_mcp import client as client_module
from sfdc_mcp.client import SfdcClient, SfdcError

INSTANCE_URL = "https://acme.my.salesforce.com"
API_VERSION = "62.0"
BASE = f"{INSTANCE_URL}/services/data/v{API_VERSION}"

TOKEN_MARKER = "s3cret-access-token"
REFRESHED_TOKEN_MARKER = "s3cret-refreshed-token"


class FakeAuth:
    """Minimal AuthProvider stand-in. Records every get_token() call (and whether it
    was a forced refresh) so tests can assert the client's 401-recovery behavior;
    supports simulating an org migration by handing back a different instance_url
    after a forced refresh."""

    def __init__(self, instance_url: str = INSTANCE_URL, refreshed_instance_url: str | None = None):
        self.token = TOKEN_MARKER
        self.refreshed_token = REFRESHED_TOKEN_MARKER
        self._instance_url = instance_url
        self._refreshed_instance_url = refreshed_instance_url
        self.force_refresh_calls = 0
        self.get_token_calls: list[bool] = []
        # Real AuthProvider contract: instance_url() raises until get_token() has
        # succeeded at least once — see AuthProvider.instance_url's docstring.
        self._has_succeeded = False

    def get_token(self, force_refresh: bool = False) -> str:
        self.get_token_calls.append(force_refresh)
        if force_refresh:
            self.force_refresh_calls += 1
            self.token = self.refreshed_token
            if self._refreshed_instance_url is not None:
                self._instance_url = self._refreshed_instance_url
        self._has_succeeded = True
        return self.token

    def instance_url(self) -> str:
        if not self._has_succeeded:
            raise RuntimeError(
                "instance_url() was called before any get_token() succeeded — sign "
                "in first."
            )
        return self._instance_url


@pytest.fixture()
def fake_sleep(monkeypatch):
    """Replaces client.sleep so retry/backoff tests run instantly while recording
    every duration the client asked to sleep for, in call order."""
    calls: list[float] = []
    monkeypatch.setattr(client_module, "sleep", lambda seconds: calls.append(seconds))
    return calls


def _client(handler, auth=None, **kwargs) -> SfdcClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return SfdcClient(auth or FakeAuth(), API_VERSION, http=http, **kwargs)


# -- get: URL building, param encoding, dict/list bodies ----------------------------


def test_get_builds_url_from_instance_url_and_api_version(fake_sleep):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"Id": "001xx"})

    client = _client(handler)
    client.get("/sobjects/Account/001xx")

    assert captured["url"] == f"{BASE}/sobjects/Account/001xx"


def test_get_params_are_url_encoded_not_concatenated(fake_sleep):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"totalSize": 0, "done": True, "records": []})

    client = _client(handler)
    client.get("/query", params={"q": "SELECT Id FROM Account WHERE Name='A&B'"})

    assert "Name%3D%27A%26B%27" in captured["url"]


def test_get_returns_dict_body(fake_sleep):
    def handler(request):
        return httpx.Response(200, json={"Id": "001xx", "Name": "Acme"})

    client = _client(handler)

    assert client.get("/sobjects/Account/001xx") == {"Id": "001xx", "Name": "Acme"}


def test_get_returns_list_body(fake_sleep):
    """Some Salesforce endpoints answer with a bare JSON array on success — get()
    must hand that back as-is rather than assuming every success body is a dict."""

    def handler(request):
        return httpx.Response(200, json=[{"label": "REST"}, {"label": "SOAP"}])

    client = _client(handler)

    assert client.get("/sobjects/Account/describe/whatever") == [
        {"label": "REST"},
        {"label": "SOAP"},
    ]


def test_authorization_header_is_bearer_token(fake_sleep):
    captured = {}

    def handler(request):
        captured["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"Id": "001xx"})

    client = _client(handler)
    client.get("/sobjects/Account/001xx")

    assert captured["auth"] == f"Bearer {TOKEN_MARKER}"


def test_default_http_client_sets_timeout():
    client = SfdcClient(FakeAuth(), API_VERSION, timeout_seconds=30.0)

    timeout = client._http.timeout
    assert timeout.read == 30.0
    assert timeout.connect == 5.0


# -- 429/503 retry matrix ------------------------------------------------------------


def test_429_with_retry_after_recovers(fake_sleep):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json=[{"message": "limit"}])
        return httpx.Response(200, json={"totalSize": 0, "done": True, "records": []})

    client = _client(handler)

    client.get("/query", params={"q": "SELECT Id FROM Account"})

    assert fake_sleep == [2.0]


def test_429_four_times_raises_after_three_retries(fake_sleep):
    def handler(request):
        return httpx.Response(
            429,
            headers={"Retry-After": "1"},
            json=[{"message": "Request limit exceeded.", "errorCode": "REQUEST_LIMIT_EXCEEDED"}],
        )

    client = _client(handler)

    with pytest.raises(SfdcError) as exc_info:
        client.get("/sobjects/Account/001xx")

    assert exc_info.value.status == 429
    assert fake_sleep == [1.0, 1.0, 1.0]


def test_429_non_numeric_retry_after_falls_back_to_backoff(fake_sleep):
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "soon"}, json=[{"message": "x"}])

    client = _client(handler)

    with pytest.raises(SfdcError):
        client.get("/sobjects/Account/001xx")

    assert fake_sleep == [1, 2, 4]


def test_retry_after_huge_value_clamped_to_sixty(fake_sleep):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3600"}, json=[{"message": "x"}])
        return httpx.Response(200, json={"Id": "001xx"})

    client = _client(handler)
    client.get("/sobjects/Account/001xx")

    assert fake_sleep == [60.0]


def test_retry_after_negative_value_clamped_to_zero(fake_sleep):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "-5"}, json=[{"message": "x"}])
        return httpx.Response(200, json={"Id": "001xx"})

    client = _client(handler)
    client.get("/sobjects/Account/001xx")

    assert fake_sleep == [0.0]


def test_503_fallback_backoff_then_recovers(fake_sleep):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 4:
            return httpx.Response(503, json=[{"message": "unavailable"}])
        return httpx.Response(200, json={"Id": "001xx"})

    client = _client(handler)
    client.get("/sobjects/Account/001xx")

    assert fake_sleep == [1, 2, 4]


def test_500_502_504_are_not_retried(fake_sleep):
    for status in (500, 502, 504):

        def handler(request, status=status):
            return httpx.Response(status, json=[{"message": "server error"}])

        client = _client(handler)

        with pytest.raises(SfdcError) as exc_info:
            client.get("/sobjects/Account/001xx")

        assert exc_info.value.status == status
        assert "not retried" in exc_info.value.message
    assert fake_sleep == []


# -- timeout / unreachable ------------------------------------------------------------


def test_timeout_maps_to_timed_out_not_unreachable(fake_sleep):
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(handler, timeout_seconds=7.0)

    with pytest.raises(SfdcError, match="Salesforce timed out after 7s") as exc_info:
        client.get("/sobjects/Account/001xx")

    assert "unreachable" not in exc_info.value.message
    assert exc_info.value.status is None


def test_network_error_maps_to_unreachable(fake_sleep):
    def handler(request):
        raise httpx.ConnectError("Connection refused", request=request)

    client = _client(handler)

    with pytest.raises(SfdcError, match="Salesforce unreachable") as exc_info:
        client.get("/sobjects/Account/001xx")

    assert exc_info.value.status is None


# -- non-JSON / 3xx guard -------------------------------------------------------------


def test_html_200_body_maps_to_non_json_sentence(fake_sleep):
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><body>Please log in</body></html>",
        )

    client = _client(handler)

    with pytest.raises(SfdcError) as exc_info:
        client.get("/sobjects/Account/001xx")

    assert exc_info.value.status == 200
    assert "non-JSON" in exc_info.value.message
    assert "SFDC_MCP_LOGIN_URL" in exc_info.value.message


def test_non_dict_non_list_json_body_maps_to_non_json_sentence(fake_sleep):
    def handler(request):
        return httpx.Response(200, json="just a string")

    client = _client(handler)

    with pytest.raises(SfdcError) as exc_info:
        client.get("/sobjects/Account/001xx")

    assert "non-JSON" in exc_info.value.message


def test_302_redirect_maps_to_non_json_sentence(fake_sleep):
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://login.salesforce.com/"})

    client = _client(handler)

    with pytest.raises(SfdcError) as exc_info:
        client.get("/sobjects/Account/001xx")

    assert exc_info.value.status == 302
    assert "non-JSON" in exc_info.value.message
    assert "maintenance" in exc_info.value.message


# -- error-array folding --------------------------------------------------------------


def test_403_request_limit_exceeded_gives_actionable_sentence(fake_sleep):
    def handler(request):
        return httpx.Response(
            403,
            json=[
                {
                    "message": "TotalRequests Limit exceeded.",
                    "errorCode": "REQUEST_LIMIT_EXCEEDED",
                }
            ],
        )

    client = _client(handler)

    with pytest.raises(SfdcError) as exc_info:
        client.get("/sobjects/Account/001xx")

    assert exc_info.value.status == 403
    message = exc_info.value.message
    assert "daily API request limit" in message
    assert "24-hour" in message
    assert "System Overview" in message


def test_403_other_names_sobject_when_inferable(fake_sleep):
    def handler(request):
        return httpx.Response(
            403, json=[{"message": "insufficient access", "errorCode": "INSUFFICIENT_ACCESS"}]
        )

    client = _client(handler)

    with pytest.raises(SfdcError) as exc_info:
        client.get("/sobjects/Account/describe")

    assert exc_info.value.status == 403
    assert "Account" in exc_info.value.message


def test_403_without_inferable_sobject_uses_generic_sentence(fake_sleep):
    def handler(request):
        return httpx.Response(
            403, json=[{"message": "insufficient access", "errorCode": "INSUFFICIENT_ACCESS"}]
        )

    client = _client(handler)

    with pytest.raises(SfdcError) as exc_info:
        client.get("/parameterizedSearch", params={"q": "acme"})

    assert exc_info.value.status == 403
    assert "this request" in exc_info.value.message


def test_404_on_record_path_names_record_and_sobject(fake_sleep):
    def handler(request):
        return httpx.Response(404, json=[{"message": "Not Found", "errorCode": "NOT_FOUND"}])

    client = _client(handler)

    with pytest.raises(SfdcError) as exc_info:
        client.get("/sobjects/Account/001xx0000006abcAAA")

    message = exc_info.value.message
    assert "001xx0000006abcAAA" in message
    assert "Account" in message
    assert "does not exist" not in message


def test_404_on_describe_path_names_sobject_as_missing(fake_sleep):
    def handler(request):
        return httpx.Response(404, json=[{"message": "Not Found", "errorCode": "NOT_FOUND"}])

    client = _client(handler)

    with pytest.raises(SfdcError) as exc_info:
        client.get("/sobjects/Frobnicator/describe")

    message = exc_info.value.message
    assert "Frobnicator" in message
    assert "does not exist" in message


def test_400_malformed_query_prefixes_soql_error_with_salesforce_message(fake_sleep):
    def handler(request):
        return httpx.Response(
            400,
            json=[
                {
                    "message": "unexpected token: 'FORM'",
                    "errorCode": "MALFORMED_QUERY",
                }
            ],
        )

    client = _client(handler)

    with pytest.raises(SfdcError) as exc_info:
        client.get("/query", params={"q": "SELECT Id FORM Account"})

    assert exc_info.value.status == 400
    message = exc_info.value.message
    assert message.startswith("SOQL error: ")
    assert "unexpected token" in message


def test_generic_error_folds_message_and_error_code(fake_sleep):
    def handler(request):
        return httpx.Response(
            400,
            json=[{"message": "Invalid field", "errorCode": "INVALID_FIELD"}],
        )

    client = _client(handler)

    with pytest.raises(SfdcError) as exc_info:
        client.get("/query", params={"q": "SELECT Bogus FROM Account"})

    assert exc_info.value.status == 400
    assert "Invalid field" in exc_info.value.message
    assert "INVALID_FIELD" in exc_info.value.message


@pytest.mark.parametrize("status", [400, 403, 404, 500])
def test_empty_json_array_error_body_does_not_crash(fake_sleep, status):
    """`_fold_error_body`'s `body and isinstance(body[0], dict)` guard is falsy for an
    empty array (Salesforce sometimes answers an error status with a bare `[]` rather
    than the usual one-entry array), yielding (None, None) — every status branch in
    `_error_for` must tolerate that fold without crashing."""

    def handler(request):
        return httpx.Response(status, json=[])

    client = _client(handler)

    with pytest.raises(SfdcError) as exc_info:
        client.get("/sobjects/Account/001xx")

    assert exc_info.value.status == status
    assert exc_info.value.message


# -- 401 refresh-once -----------------------------------------------------------------


def test_401_triggers_one_force_refresh_then_succeeds(fake_sleep):
    attempts = {"n": 0}
    auth = FakeAuth()

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            assert request.headers["authorization"] == f"Bearer {TOKEN_MARKER}"
            return httpx.Response(401, json=[{"message": "Session expired"}])
        assert request.headers["authorization"] == f"Bearer {REFRESHED_TOKEN_MARKER}"
        return httpx.Response(200, json={"Id": "001xx"})

    client = _client(handler, auth=auth)

    assert client.get("/sobjects/Account/001xx") == {"Id": "001xx"}
    assert auth.force_refresh_calls == 1
    assert attempts["n"] == 2


def test_401_twice_raises_sign_in_again_without_leaking_token(fake_sleep):
    auth = FakeAuth()

    def handler(request):
        return httpx.Response(401, json=[{"message": "Session expired"}])

    client = _client(handler, auth=auth)

    with pytest.raises(SfdcError) as exc_info:
        client.get("/sobjects/Account/001xx")

    assert exc_info.value.status == 401
    assert "sign in again" in exc_info.value.message
    assert auth.force_refresh_calls == 1
    assert TOKEN_MARKER not in exc_info.value.message
    assert REFRESHED_TOKEN_MARKER not in exc_info.value.message


def test_401_refresh_re_reads_instance_url_for_retried_request(fake_sleep):
    """An org migration can change instance_url as a side effect of a token
    refresh. The retried request after a 401 must be built against the *new*
    instance_url, not the one cached before the refresh."""
    new_instance = "https://acme-migrated.my.salesforce.com"
    auth = FakeAuth(refreshed_instance_url=new_instance)
    urls = []

    def handler(request):
        urls.append(str(request.url))
        if len(urls) == 1:
            return httpx.Response(401, json=[{"message": "Session expired"}])
        return httpx.Response(200, json={"Id": "001xx"})

    client = _client(handler, auth=auth)
    client.get("/sobjects/Account/001xx")

    assert urls[0] == f"{BASE}/sobjects/Account/001xx"
    assert urls[1] == f"{new_instance}/services/data/v{API_VERSION}/sobjects/Account/001xx"


# -- Sforce-Limit-Info parsing + usage_note thresholds ---------------------------------


def test_usage_note_none_below_ninety_percent(fake_sleep):
    def handler(request):
        return httpx.Response(
            200, headers={"Sforce-Limit-Info": "api-usage=13350/15000"}, json={"Id": "001xx"}
        )

    client = _client(handler)
    client.get("/sobjects/Account/001xx")

    assert client.usage_note() is None


def test_usage_note_present_at_ninety_percent(fake_sleep):
    def handler(request):
        return httpx.Response(
            200, headers={"Sforce-Limit-Info": "api-usage=13500/15000"}, json={"Id": "001xx"}
        )

    client = _client(handler)
    client.get("/sobjects/Account/001xx")

    note = client.usage_note()
    assert note is not None
    assert "13500/15000" in note


def test_usage_note_none_when_header_absent(fake_sleep):
    def handler(request):
        return httpx.Response(200, json={"Id": "001xx"})

    client = _client(handler)
    client.get("/sobjects/Account/001xx")

    assert client.usage_note() is None


def test_malformed_limit_info_header_is_silently_ignored(fake_sleep):
    def handler(request):
        return httpx.Response(
            200, headers={"Sforce-Limit-Info": "not-a-usage-header"}, json={"Id": "001xx"}
        )

    client = _client(handler)
    client.get("/sobjects/Account/001xx")

    assert client.usage_note() is None


def test_limit_info_ignores_per_app_usage_ordered_first(fake_sleep):
    """Salesforce can send both a per-app and an org-wide usage entry on the same
    Sforce-Limit-Info header; `per-app-api-usage=...` must not be mistaken for
    `api-usage=...` when it happens to be ordered first (an unanchored `api-usage=`
    regex would match inside `per-app-api-usage=` too)."""

    def handler(request):
        return httpx.Response(
            200,
            headers={"Sforce-Limit-Info": "per-app-api-usage=500/1000;api-usage=13500/15000"},
            json={"Id": "001xx"},
        )

    client = _client(handler)
    client.get("/sobjects/Account/001xx")

    note = client.usage_note()
    assert note is not None
    assert "13500/15000" in note


def test_limit_info_parsed_even_on_error_response(fake_sleep):
    def handler(request):
        return httpx.Response(
            404,
            headers={"Sforce-Limit-Info": "api-usage=14000/15000"},
            json=[{"message": "Not Found"}],
        )

    client = _client(handler)
    with pytest.raises(SfdcError):
        client.get("/sobjects/Account/001xx")

    assert client.usage_note() is not None


# -- query_paged: pagination, nextRecordsUrl chain, cap, totalSize --------------------


def test_query_paged_single_page(fake_sleep):
    def handler(request):
        assert str(request.url).startswith(f"{BASE}/query")
        return httpx.Response(
            200,
            json={
                "totalSize": 2,
                "done": True,
                "records": [{"Id": "1"}, {"Id": "2"}],
            },
        )

    client = _client(handler)
    records, total = client.query_paged("SELECT Id FROM Account", limit=25)

    assert [r["Id"] for r in records] == ["1", "2"]
    assert total == 2


def test_query_paged_follows_next_records_url_verbatim(fake_sleep):
    next_path = f"/services/data/v{API_VERSION}/query/01gXX-2000"
    urls = []

    def handler(request):
        urls.append(str(request.url))
        if len(urls) == 1:
            return httpx.Response(
                200,
                json={
                    "totalSize": 4,
                    "done": False,
                    "nextRecordsUrl": next_path,
                    "records": [{"Id": "1"}, {"Id": "2"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "totalSize": 4,
                "done": True,
                "records": [{"Id": "3"}, {"Id": "4"}],
            },
        )

    client = _client(handler)
    records, total = client.query_paged("SELECT Id FROM Account", limit=25)

    assert [r["Id"] for r in records] == ["1", "2", "3", "4"]
    assert total == 4
    assert urls[1] == f"{INSTANCE_URL}{next_path}"


def test_query_paged_stops_at_limit_without_fetching_further_pages(fake_sleep):
    next_path = f"/services/data/v{API_VERSION}/query/01gXX-2000"
    requests_made = []

    def handler(request):
        requests_made.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "totalSize": 10,
                "done": False,
                "nextRecordsUrl": next_path,
                "records": [{"Id": "1"}, {"Id": "2"}, {"Id": "3"}],
            },
        )

    client = _client(handler)
    records, total = client.query_paged("SELECT Id FROM Account", limit=3)

    assert [r["Id"] for r in records] == ["1", "2", "3"]
    assert total == 10
    assert len(requests_made) == 1


def test_query_paged_caps_records_even_when_page_overshoots_limit(fake_sleep):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "totalSize": 5,
                "done": True,
                "records": [{"Id": str(i)} for i in range(5)],
            },
        )

    client = _client(handler)
    records, total = client.query_paged("SELECT Id FROM Account", limit=2)

    assert [r["Id"] for r in records] == ["0", "1"]
    assert total == 5


def test_query_paged_defaults_limit_to_item_limit(fake_sleep):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "totalSize": 3,
                "done": True,
                "records": [{"Id": str(i)} for i in range(3)],
            },
        )

    client = _client(handler, item_limit=2)
    records, _total = client.query_paged("SELECT Id FROM Account")

    assert len(records) == 2


def test_query_paged_error_on_first_page_propagates(fake_sleep):
    def handler(request):
        return httpx.Response(400, json=[{"message": "bad soql", "errorCode": "MALFORMED_QUERY"}])

    client = _client(handler)

    with pytest.raises(SfdcError) as exc_info:
        client.query_paged("SELECT FORM Account")

    assert exc_info.value.status == 400
    assert exc_info.value.message.startswith("SOQL error: ")


def test_query_paged_error_on_subsequent_page_propagates(fake_sleep):
    next_path = f"/services/data/v{API_VERSION}/query/01gXX-2000"
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "totalSize": 4,
                    "done": False,
                    "nextRecordsUrl": next_path,
                    "records": [{"Id": "1"}],
                },
            )
        return httpx.Response(500, json=[{"message": "server error"}])

    client = _client(handler)

    with pytest.raises(SfdcError) as exc_info:
        client.query_paged("SELECT Id FROM Account", limit=25)

    assert exc_info.value.status == 500
