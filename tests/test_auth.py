import json
import os
import stat
import threading
from urllib.parse import parse_qsl

import httpx
import pytest

from sfdc_mcp import auth as auth_module
from sfdc_mcp.auth import (
    AuthError,
    ClientCredentialsAuth,
    DeviceCodeAuth,
    LoginRequired,
    build_auth,
)
from sfdc_mcp.settings import Settings

LOGIN_URL = "https://login.salesforce.com"

DEVICE_FLOW = {
    "device_code": "long-device-code",
    "user_code": "X1D9SEET",
    "verification_uri": "https://acme.my.salesforce.com/setup/connect",
    "interval": 5,
}

SECRET_MARKERS = ["s3cret-client-secret", "s3cret-refresh-token", "s3cret-access-token"]


def _body(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.content.decode()))


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture()
def fake_sleep(monkeypatch):
    """Replaces auth.sleep so polling tests run instantly while recording every
    duration the provider asked to sleep for, in call order."""
    calls: list[float] = []
    monkeypatch.setattr(auth_module, "sleep", lambda seconds: calls.append(seconds))
    return calls


def _device_auth(handler, cache_path, **kwargs) -> DeviceCodeAuth:
    return DeviceCodeAuth(
        client_id="client-id",
        login_url=LOGIN_URL,
        token_cache_path=str(cache_path),
        http=_client(handler),
        **kwargs,
    )


def _seed_cache(cache_path, **fields) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(fields))


# -- DeviceCodeAuth: initiate / LoginRequired --------------------------------------


def test_get_token_without_cache_raises_login_required_with_url_and_code(tmp_path):
    def handler(request):
        return httpx.Response(200, json=DEVICE_FLOW)

    provider = _device_auth(handler, tmp_path / "cache.json")

    with pytest.raises(LoginRequired) as exc_info:
        provider.get_token()

    message = str(exc_info.value)
    assert "X1D9SEET" in message
    assert "https://acme.my.salesforce.com/setup/connect" in message
    assert provider.login_instructions() == message


def test_get_token_reuses_pending_flow_instead_of_reinitiating(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=DEVICE_FLOW)

    provider = _device_auth(handler, tmp_path / "cache.json")

    with pytest.raises(LoginRequired):
        provider.get_token()
    with pytest.raises(LoginRequired):
        provider.get_token()

    assert len(calls) == 1


def test_initiate_failure_names_client_id(tmp_path):
    def handler(request):
        return httpx.Response(
            400,
            json={"error": "invalid_client_id", "error_description": "client identifier invalid"},
        )

    provider = _device_auth(handler, tmp_path / "cache.json")

    with pytest.raises(LoginRequired, match="SFDC_MCP_CLIENT_ID"):
        provider.get_token()


# -- DeviceCodeAuth: complete_login / polling ----------------------------------------


def test_complete_login_happy_path_persists_cache_at_0600(tmp_path, fake_sleep):
    def handler(request):
        body = _body(request)
        if body.get("response_type") == "device_code":
            return httpx.Response(200, json=DEVICE_FLOW)
        assert body["grant_type"] == "device"
        assert body["code"] == DEVICE_FLOW["device_code"]
        return httpx.Response(
            200,
            json={
                "access_token": "device-flow-token",
                "refresh_token": "device-flow-refresh",
                "instance_url": "https://acme.my.salesforce.com",
                "token_type": "Bearer",
            },
        )

    cache_path = tmp_path / "cache.json"
    provider = _device_auth(handler, cache_path)

    with pytest.raises(LoginRequired):
        provider.get_token()
    token = provider.complete_login()

    assert token == "device-flow-token"
    assert provider.login_instructions() is None
    assert provider.instance_url() == "https://acme.my.salesforce.com"
    assert cache_path.exists()
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600
    cached = json.loads(cache_path.read_text())
    assert cached == {
        "access_token": "device-flow-token",
        "refresh_token": "device-flow-refresh",
        "instance_url": "https://acme.my.salesforce.com",
    }
    assert not (tmp_path / "cache.json.tmp").exists()


def test_complete_login_polls_through_authorization_pending(tmp_path, fake_sleep):
    poll_count = {"n": 0}

    def handler(request):
        body = _body(request)
        if body.get("response_type") == "device_code":
            return httpx.Response(200, json=DEVICE_FLOW)
        poll_count["n"] += 1
        if poll_count["n"] < 3:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(
            200, json={"access_token": "tok", "instance_url": "https://acme.my.salesforce.com"}
        )

    provider = _device_auth(handler, tmp_path / "cache.json")
    with pytest.raises(LoginRequired):
        provider.get_token()

    token = provider.complete_login()

    assert token == "tok"
    assert poll_count["n"] == 3
    # One sleep(interval) precedes each poll attempt, at the server-given interval.
    assert fake_sleep == [5, 5, 5]


def test_complete_login_slow_down_increases_interval_by_five(tmp_path, fake_sleep):
    poll_count = {"n": 0}

    def handler(request):
        body = _body(request)
        if body.get("response_type") == "device_code":
            return httpx.Response(200, json=DEVICE_FLOW)
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            return httpx.Response(400, json={"error": "slow_down"})
        return httpx.Response(
            200, json={"access_token": "tok", "instance_url": "https://acme.my.salesforce.com"}
        )

    provider = _device_auth(handler, tmp_path / "cache.json")
    with pytest.raises(LoginRequired):
        provider.get_token()

    token = provider.complete_login()

    assert token == "tok"
    assert fake_sleep == [5, 10]


def test_complete_login_terminal_error_clears_flow_and_next_get_token_mints_fresh_flow(
    tmp_path, fake_sleep
):
    """Salesforce folds an expired/disabled device code into invalid_grant (or
    invalid_request) rather than a distinct expired_token code — see the module
    docstring. Either way it's terminal: the dead flow must be discarded so the next
    get_token() starts a brand-new device code rather than re-polling a dead one."""
    initiate_calls = []

    def handler(request):
        body = _body(request)
        if body.get("response_type") == "device_code":
            initiate_calls.append(1)
            return httpx.Response(200, json=DEVICE_FLOW)
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "device flow expired"},
        )

    provider = _device_auth(handler, tmp_path / "cache.json")
    with pytest.raises(LoginRequired):
        provider.get_token()

    with pytest.raises(LoginRequired, match="device flow expired"):
        provider.complete_login()

    assert provider.login_instructions() is None
    assert len(initiate_calls) == 1

    with pytest.raises(LoginRequired):
        provider.get_token()

    assert len(initiate_calls) == 2  # a fresh flow was minted, the dead one wasn't reused


def test_complete_login_without_pending_flow_raises(tmp_path):
    def handler(request):
        raise AssertionError("no request should be made")

    provider = _device_auth(handler, tmp_path / "cache.json")

    with pytest.raises(LoginRequired, match="get_token"):
        provider.complete_login()


def test_complete_login_times_out_if_never_approved(tmp_path, fake_sleep):
    def handler(request):
        body = _body(request)
        if body.get("response_type") == "device_code":
            return httpx.Response(200, json=DEVICE_FLOW)
        return httpx.Response(400, json={"error": "authorization_pending"})

    provider = _device_auth(handler, tmp_path / "cache.json", poll_timeout_seconds=0.0)
    with pytest.raises(LoginRequired):
        provider.get_token()

    with pytest.raises(LoginRequired, match="timed out"):
        provider.complete_login()


def test_get_token_during_inflight_completion_reuses_same_pending_instructions(
    tmp_path, monkeypatch
):
    """The race this guards against: a tool call's get_token() lands while the
    background thread's complete_login() is mid-poll. Before the held-flow fix,
    complete_login() would clear self._flow the instant it started, so a concurrent
    get_token() would see no pending flow and mint a brand-new device code —
    abandoning the code the background thread is still polling for. It must instead
    see the SAME pending instructions and must not initiate a second device code."""
    monkeypatch.setattr(auth_module, "sleep", lambda seconds: None)
    initiate_calls = []
    started = threading.Event()
    release = threading.Event()

    def handler(request):
        body = _body(request)
        if body.get("response_type") == "device_code":
            initiate_calls.append(1)
            return httpx.Response(200, json=DEVICE_FLOW)
        started.set()
        release.wait(timeout=2)
        return httpx.Response(
            200, json={"access_token": "tok", "instance_url": "https://acme.my.salesforce.com"}
        )

    provider = _device_auth(handler, tmp_path / "cache.json")
    with pytest.raises(LoginRequired) as exc_info:
        provider.get_token()
    first_message = str(exc_info.value)

    completion_thread = threading.Thread(target=provider.complete_login)
    completion_thread.start()
    assert started.wait(timeout=2)

    with pytest.raises(LoginRequired) as exc_info2:
        provider.get_token()

    assert str(exc_info2.value) == first_message
    assert len(initiate_calls) == 1

    release.set()
    completion_thread.join(timeout=2)


# -- DeviceCodeAuth: cache ------------------------------------------------------------


def test_cache_loaded_from_disk_lets_get_token_skip_network(tmp_path):
    cache_path = tmp_path / "cache.json"
    _seed_cache(
        cache_path,
        access_token="cached-token",
        refresh_token="cached-refresh",
        instance_url="https://acme.my.salesforce.com",
    )

    def handler(request):
        raise AssertionError("no request should be made")

    provider = _device_auth(handler, cache_path)

    assert provider.get_token() == "cached-token"
    assert provider.instance_url() == "https://acme.my.salesforce.com"


def test_cached_access_token_without_instance_url_is_not_returned(tmp_path):
    """A partial/hand-edited cache (access_token present, instance_url missing) must
    not be handed out as a usable token — SfdcClient would crash on instance_url() on
    the very next request. get_token() must treat this exactly like no cache at all
    (here: fall through to LoginRequired, since there's no refresh_token either)."""
    cache_path = tmp_path / "cache.json"
    _seed_cache(cache_path, access_token="stale-token-no-instance-url")

    def handler(request):
        return httpx.Response(200, json=DEVICE_FLOW)

    provider = _device_auth(handler, cache_path)

    with pytest.raises(LoginRequired):
        provider.get_token()


def test_corrupt_cache_file_warns_and_starts_with_empty_cache(tmp_path, capsys):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{not valid json at all!!")

    def handler(request):
        raise AssertionError("no request should be made")

    provider = _device_auth(handler, cache_path)

    assert provider is not None  # construction must not raise
    captured = capsys.readouterr()
    assert str(cache_path) in captured.err
    assert "corrupt" in captured.err


def test_unreadable_cache_file_warns_and_starts_with_empty_cache(tmp_path, capsys):
    if os.geteuid() == 0:
        pytest.skip("root ignores file permission bits, so this can't reproduce as root")

    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"access_token": "x"}))
    cache_path.chmod(0)

    def handler(request):
        raise AssertionError("no request should be made")

    try:
        provider = _device_auth(handler, cache_path)
    finally:
        cache_path.chmod(0o600)  # let tmp_path cleanup remove it afterward

    assert provider is not None  # construction must not raise
    captured = capsys.readouterr()
    assert str(cache_path) in captured.err


def test_persisted_cache_file_is_written_atomically_at_0600(tmp_path):
    cache_path = tmp_path / "cache.json"

    def handler(request):
        raise AssertionError("no request should be made")

    provider = _device_auth(handler, cache_path)
    provider._access_token = "tok"
    provider._persist_cache()

    assert cache_path.exists()
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600
    assert not (tmp_path / "cache.json.tmp").exists()


# -- DeviceCodeAuth: silent refresh ---------------------------------------------------


def test_get_token_silently_refreshes_using_cached_refresh_token(tmp_path):
    cache_path = tmp_path / "cache.json"
    _seed_cache(cache_path, refresh_token="s3cret-refresh-token")
    requests = []

    def handler(request):
        body = _body(request)
        requests.append(body)
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "s3cret-refresh-token"
        return httpx.Response(
            200, json={"access_token": "refreshed-token", "instance_url": "https://acme.my.salesforce.com"}
        )

    provider = _device_auth(handler, cache_path)

    assert provider.get_token() == "refreshed-token"
    assert len(requests) == 1


def test_refresh_grant_keeps_old_refresh_token_when_not_rotated(tmp_path):
    cache_path = tmp_path / "cache.json"
    _seed_cache(cache_path, refresh_token="original-refresh")

    def handler(request):
        # No refresh_token in the response — rotation is off.
        return httpx.Response(
            200, json={"access_token": "tok", "instance_url": "https://acme.my.salesforce.com"}
        )

    provider = _device_auth(handler, cache_path)
    provider.get_token()

    cached = json.loads(cache_path.read_text())
    assert cached["refresh_token"] == "original-refresh"


def test_refresh_failure_clears_refresh_token_and_falls_through_to_login_required(tmp_path):
    cache_path = tmp_path / "cache.json"
    _seed_cache(cache_path, refresh_token="dead-refresh")
    initiate_calls = []

    def handler(request):
        body = _body(request)
        if body.get("grant_type") == "refresh_token":
            return httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "expired access/refresh token",
                },
            )
        initiate_calls.append(1)
        return httpx.Response(200, json=DEVICE_FLOW)

    provider = _device_auth(handler, cache_path)

    with pytest.raises(LoginRequired):
        provider.get_token()

    assert len(initiate_calls) == 1
    cached = json.loads(cache_path.read_text())
    assert cached["refresh_token"] is None
    assert cached["access_token"] is None


def test_force_refresh_uses_refresh_grant_even_with_cached_access_token(tmp_path):
    cache_path = tmp_path / "cache.json"
    _seed_cache(cache_path, access_token="stale-token", refresh_token="s3cret-refresh-token")

    def handler(request):
        body = _body(request)
        assert body["grant_type"] == "refresh_token"
        return httpx.Response(
            200, json={"access_token": "fresh-token", "instance_url": "https://acme.my.salesforce.com"}
        )

    provider = _device_auth(handler, cache_path)

    assert provider.get_token(force_refresh=True) == "fresh-token"


def test_force_refresh_without_refresh_token_raises_login_required(tmp_path):
    cache_path = tmp_path / "cache.json"
    _seed_cache(cache_path, access_token="stale-token")

    def handler(request):
        return httpx.Response(200, json=DEVICE_FLOW)

    provider = _device_auth(handler, cache_path)

    with pytest.raises(LoginRequired):
        provider.get_token(force_refresh=True)


# -- DeviceCodeAuth: instance_url ------------------------------------------------------


def test_instance_url_before_login_raises(tmp_path):
    def handler(request):
        raise AssertionError("no request should be made")

    provider = _device_auth(handler, tmp_path / "cache.json")

    with pytest.raises(RuntimeError):
        provider.instance_url()


# -- ClientCredentialsAuth -------------------------------------------------------------


def _cc_auth(handler) -> ClientCredentialsAuth:
    return ClientCredentialsAuth(
        client_id="client-id",
        client_secret="s3cret-client-secret",
        login_url=LOGIN_URL,
        http=_client(handler),
    )


def test_client_credentials_returns_token_and_instance_url():
    def handler(request):
        body = _body(request)
        assert body["grant_type"] == "client_credentials"
        assert body["client_secret"] == "s3cret-client-secret"
        return httpx.Response(
            200,
            json={
                "access_token": "s3cret-access-token",
                "instance_url": "https://acme.my.salesforce.com",
                "token_type": "Bearer",
            },
        )

    provider = _cc_auth(handler)

    assert provider.get_token() == "s3cret-access-token"
    assert provider.instance_url() == "https://acme.my.salesforce.com"


def test_client_credentials_invalid_client_names_env_vars():
    def handler(request):
        return httpx.Response(
            400, json={"error": "invalid_client", "error_description": "client secret invalid"}
        )

    provider = _cc_auth(handler)

    with pytest.raises(AuthError, match="SFDC_MCP_CLIENT_ID") as exc_info:
        provider.get_token()
    assert "SFDC_MCP_CLIENT_SECRET" in str(exc_info.value)


def test_client_credentials_other_error_mentions_my_domain():
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "disabled"})

    provider = _cc_auth(handler)

    with pytest.raises(AuthError, match="My Domain"):
        provider.get_token()


def test_client_credentials_default_does_not_refetch():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            200, json={"access_token": "tok", "instance_url": "https://acme.my.salesforce.com"}
        )

    provider = _cc_auth(handler)
    provider.get_token()
    provider.get_token()

    assert len(calls) == 1


def test_client_credentials_force_refresh_refetches():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            200,
            json={"access_token": f"tok-{len(calls)}", "instance_url": "https://acme.my.salesforce.com"},
        )

    provider = _cc_auth(handler)
    first = provider.get_token()
    second = provider.get_token(force_refresh=True)

    assert first != second
    assert len(calls) == 2


def test_client_credentials_instance_url_before_login_raises():
    def handler(request):
        raise AssertionError("no request should be made")

    provider = _cc_auth(handler)

    with pytest.raises(RuntimeError):
        provider.instance_url()


# -- build_auth --------------------------------------------------------------------


def test_build_auth_builds_device_code_provider(tmp_path):
    settings = Settings(
        auth="device_code", client_id="c", token_cache_path=str(tmp_path / "cache.json")
    )

    provider = build_auth(settings, http=_client(lambda r: httpx.Response(200, json={})))

    assert isinstance(provider, DeviceCodeAuth)


def test_build_auth_builds_client_credentials_provider():
    settings = Settings(auth="client_credentials", client_id="c", client_secret="s")

    provider = build_auth(settings, http=_client(lambda r: httpx.Response(200, json={})))

    assert isinstance(provider, ClientCredentialsAuth)


# -- misc ----------------------------------------------------------------------------


def test_auth_error_and_login_required_are_distinct_exceptions():
    assert not issubclass(LoginRequired, AuthError)
    assert not issubclass(AuthError, LoginRequired)


def test_no_secrets_leak_into_any_error_message(tmp_path):
    """Every error path here is exercised with a recognizable secret literal in
    play, and none of str(exc) across the whole battery may contain it — mirrors the
    plan's "no credentials in any error message" constraint."""
    messages: list[str] = []

    # 1. Client-credentials invalid_client.
    def cc_invalid_client(request):
        return httpx.Response(
            400, json={"error": "invalid_client", "error_description": "bad secret"}
        )

    provider = _cc_auth(cc_invalid_client)
    with pytest.raises(AuthError) as exc_info:
        provider.get_token()
    messages.append(str(exc_info.value))

    # 2. Client-credentials generic error.
    def cc_generic(request):
        return httpx.Response(400, json={"error": "server_error", "error_description": "boom"})

    provider = _cc_auth(cc_generic)
    with pytest.raises(AuthError) as exc_info:
        provider.get_token()
    messages.append(str(exc_info.value))

    # 3. Device-flow refresh failure.
    cache_path = tmp_path / "cache.json"
    _seed_cache(cache_path, refresh_token="s3cret-refresh-token")

    def device_refresh_fail(request):
        body = _body(request)
        if body.get("grant_type") == "refresh_token":
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "dead"})
        return httpx.Response(200, json=DEVICE_FLOW)

    provider = _device_auth(device_refresh_fail, cache_path)
    with pytest.raises(LoginRequired) as exc_info:
        provider.get_token()
    messages.append(str(exc_info.value))

    # 4. Device-flow initiate failure.
    def device_initiate_fail(request):
        return httpx.Response(400, json={"error": "invalid_client_id", "error_description": "bad"})

    provider = _device_auth(device_initiate_fail, tmp_path / "cache2.json")
    with pytest.raises(LoginRequired) as exc_info:
        provider.get_token()
    messages.append(str(exc_info.value))

    # 5. Device-flow complete_login terminal failure.
    def device_terminal(request):
        body = _body(request)
        if body.get("response_type") == "device_code":
            return httpx.Response(200, json=DEVICE_FLOW)
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "dead code"}
        )

    provider = _device_auth(device_terminal, tmp_path / "cache3.json")
    with pytest.raises(LoginRequired):
        provider.get_token()
    with pytest.raises(LoginRequired) as exc_info:
        provider.complete_login()
    messages.append(str(exc_info.value))

    for message in messages:
        for marker in SECRET_MARKERS:
            assert marker not in message


def test_complete_login_retries_through_transient_poll_failures(tmp_path, fake_sleep):
    """A network blip or non-JSON maintenance page mid-poll must NOT abandon a device
    code the user may still be approving — the loop keeps polling to success."""
    poll_count = {"n": 0}

    def handler(request):
        body = _body(request)
        if body.get("response_type") == "device_code":
            return httpx.Response(200, json=DEVICE_FLOW)
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            raise httpx.ConnectError("transient blip")  # _token_request folds to request_failed
        if poll_count["n"] == 2:
            return httpx.Response(503, text="<html>maintenance</html>")  # invalid_response
        return httpx.Response(
            200, json={"access_token": "tok", "instance_url": "https://acme.my.salesforce.com"}
        )

    provider = _device_auth(handler, tmp_path / "cache.json")
    with pytest.raises(LoginRequired):
        provider.get_token()

    token = provider.complete_login()

    assert token == "tok"
    assert poll_count["n"] == 3  # two transients survived, third succeeded


def test_complete_login_gives_up_after_max_consecutive_transient_failures(tmp_path, fake_sleep):
    """Bounded: a persistent transient failure eventually terminates rather than
    polling forever, surfacing an actionable LoginRequired."""
    poll_count = {"n": 0}

    def handler(request):
        body = _body(request)
        if body.get("response_type") == "device_code":
            return httpx.Response(200, json=DEVICE_FLOW)
        poll_count["n"] += 1
        return httpx.Response(503, text="down")  # every poll is a transient invalid_response

    provider = _device_auth(handler, tmp_path / "cache.json")
    with pytest.raises(LoginRequired):
        provider.get_token()
    with pytest.raises(LoginRequired):
        provider.complete_login()

    # Load-bearing: gives up after EXACTLY _MAX_TRANSIENT_POLLS, not on the first
    # transient (old behavior) and not by spinning to the 900s deadline.
    assert poll_count["n"] == 5
