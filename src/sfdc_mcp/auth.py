"""Salesforce OAuth 2.0 auth: a device-code (public client, human-interactive) provider
and a client-credentials (confidential client, service-to-service) provider, both built
on raw httpx against `{login_url}/services/oauth2/*` with an injectable http client.
Salesforce isn't Entra, so there's no MSAL here — every endpoint path, parameter name,
and response field name below was verified against live Salesforce documentation
(2026-07-12) rather than recalled from memory:

- Device-flow initiate/poll request and response shapes, and the literal
  `grant_type=device` used to poll (note: NOT the RFC 8628 value
  `urn:ietf:params:oauth:grant-type:device_code` that most device-flow
  implementations use — Salesforce's own doc example spells it `device`):
  https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_device_flow.htm&language=en_US&type=5
- Device-flow polling error codes. Salesforce's authorization-errors table has no
  RFC-8628-style `expired_token` code: `authorization_pending` and `slow_down` mean
  "keep polling"; an expired or disabled device code surfaces as `invalid_grant`
  ("the Salesforce server isn't able to grant an access token") or `invalid_request`
  ("the device code specified in the polling request is invalid"). Both, along with
  `access_denied`, are treated here as terminal — the pending flow is discarded and
  the next get_token() mints a fresh one:
  https://help.salesforce.com/s/articleView?id=xcloud.remoteaccess_oauth_flow_errors.htm&language=en_US&type=5
- Refresh-token grant request/response shape (`grant_type=refresh_token`):
  https://help.salesforce.com/s/articleView?id=xcloud.remoteaccess_oauth_refresh_token_flow.htm&language=en_US&type=5
- Client-credentials grant request/response shape (`grant_type=client_credentials`),
  including the "requires a My Domain URL, not login/test.salesforce.com" restriction
  reflected in ClientCredentialsAuth's error message:
  https://developer.salesforce.com/blogs/2023/03/using-the-client-credentials-flow-for-easier-api-authentication

Every error raised here is an actionable sentence naming the env var or Connected App
setting to fix, never a raw Salesforce error body or traceback — and never an access
token, refresh token, or client secret.
"""

import json
import os
import sys
import threading
from pathlib import Path
from time import monotonic, sleep
from typing import Protocol

import httpx

from sfdc_mcp.settings import Settings

# Scopes requested at device-flow initiation: "api" for data access, "refresh_token"
# (with "offline_access" as its alias) so a refresh token comes back and silent
# renewal is possible without re-prompting the user every couple of hours.
DEVICE_FLOW_SCOPE = "api refresh_token"

# How long complete_login() will keep polling before giving up and telling the caller
# to retry — well past the ~10 minute device_code lifetime the docs describe, since
# Salesforce doesn't return an expires_in the client could use to self-schedule.
DEFAULT_POLL_TIMEOUT_SECONDS = 900.0


class LoginRequired(Exception):
    """Raised by DeviceCodeAuth when interactive sign-in is needed. str(exc) is the
    verification-URL-and-code instructions, safe to return verbatim as an MCP tool
    result — never a stack trace, never a token."""


class AuthError(Exception):
    """Raised for a non-interactive auth failure (rejected client credentials, a
    Connected App misconfiguration). Always an actionable sentence naming the env var
    or Connected App setting to fix, never a raw Salesforce error body."""


class AuthProvider(Protocol):
    def get_token(self, force_refresh: bool = False) -> str:
        """Return a bearer token, refreshing silently if possible. Raises
        LoginRequired (device_code) or AuthError (client_credentials) when it cannot.

        `force_refresh=True` is SfdcClient's single 401-recovery attempt: the caller
        is telling us a token we previously handed out was rejected by Salesforce, so
        any cached copy must not be handed out again."""
        ...

    def instance_url(self) -> str:
        """Return the org instance URL from the most recent successful token
        response — SfdcClient's base URL for every subsequent API call. Raises if
        called before any get_token() has ever succeeded."""
        ...


# Device-flow poll outcomes that genuinely end the login (Salesforce's documented
# error table): the user denied access, or the code is invalid/expired. Everything
# else _token_request can surface (request_failed, invalid_response, server_error, an
# undocumented code) is treated as transient and retried, bounded by the count below
# and the overall poll deadline.
_TERMINAL_POLL_ERRORS = frozenset({"access_denied", "invalid_grant", "invalid_request"})
_MAX_TRANSIENT_POLLS = 5


def _token_request(http: httpx.Client, login_url: str, data: dict[str, str]) -> dict:
    """POST to {login_url}/services/oauth2/token and return the parsed JSON body,
    whether Salesforce answered success (access_token) or an OAuth error (error +
    error_description) — callers distinguish the two by checking which key is
    present, exactly like Salesforce's own examples do. A network failure or a
    non-JSON response is folded into the same {"error": ..., "error_description": ...}
    shape so every caller has exactly one failure path to handle."""
    try:
        response = http.post(f"{login_url}/services/oauth2/token", data=data)
    except httpx.HTTPError as exc:
        # Never let the underlying exception (which may echo the request, including
        # client_secret or refresh_token in the body) reach a caller.
        return {
            "error": "request_failed",
            "error_description": f"Salesforce was unreachable: {exc}",
        }
    try:
        return response.json()
    except ValueError:
        return {
            "error": "invalid_response",
            "error_description": (
                f"Salesforce returned a non-JSON response (HTTP {response.status_code})."
            ),
        }


class DeviceCodeAuth:
    """Delegated auth via Salesforce's OAuth 2.0 device flow: get_token() returns a
    cached token if one is held, otherwise silently renews via the refresh-token
    grant if one is cached, otherwise raises LoginRequired with sign-in instructions.
    complete_login() is the blocking call a background thread runs to poll Salesforce
    until the user finishes (or the login dies); see its docstring for the held-flow
    locking this mirrors from m365-mcp-server. The token cache on disk is a plain
    JSON file (access_token, refresh_token, instance_url) written 0o600, atomically."""

    def __init__(
        self,
        client_id: str,
        login_url: str,
        token_cache_path: str,
        http: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
        poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    ):
        self._client_id = client_id
        self._login_url = login_url.rstrip("/")
        self._cache_path = Path(token_cache_path).expanduser()
        self._http = (
            http
            if http is not None
            else httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=5.0))
        )
        self._poll_timeout_seconds = poll_timeout_seconds

        # Guards `_flow` and the cached tokens: a tool call's get_token() and a
        # background completion thread's complete_login() run concurrently, and
        # without a lock the two race — see complete_login's docstring.
        self._lock = threading.Lock()
        self._flow: dict | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._instance_url: str | None = None
        self._load_cache()

    def get_token(self, force_refresh: bool = False) -> str:
        # A cached access_token is only usable together with the instance_url it came
        # back with — SfdcClient's every request depends on both. A partial or
        # hand-edited cache (access_token present, instance_url missing) must never
        # be handed out: instance_url() would raise RuntimeError on the very next
        # request, escaping as a raw traceback instead of a clean LoginRequired/retry.
        # Treat that state exactly like "needs refresh or login" instead.
        if not force_refresh and self._access_token and self._instance_url is not None:
            return self._access_token

        if self._refresh_token:
            try:
                return self._refresh(self._refresh_token)
            except AuthError:
                # The cached refresh token is dead — don't keep retrying it on every
                # call; fall through to a fresh interactive login instead.
                with self._lock:
                    self._access_token = None
                    self._refresh_token = None
                self._persist_cache()

        with self._lock:
            if self._flow is None:
                self._flow = self._initiate()
            message = self._flow["message"]
        raise LoginRequired(message)

    def _initiate(self) -> dict:
        body = _token_request(
            self._http,
            self._login_url,
            {
                "response_type": "device_code",
                "client_id": self._client_id,
                "scope": DEVICE_FLOW_SCOPE,
            },
        )
        if "user_code" not in body or "verification_uri" not in body:
            detail = (
                body.get("error_description")
                or body.get("error")
                or "no details returned by Salesforce"
            )
            raise LoginRequired(
                f"Could not start device-code login ({detail}). Verify "
                "SFDC_MCP_CLIENT_ID is a valid External Client App (or Connected App) "
                "consumer key with the device flow enabled."
            )
        message = (
            f"To sign in to Salesforce, open {body['verification_uri']} in a "
            f"browser and enter the code {body['user_code']}."
        )
        return {**body, "message": message}

    def complete_login(self) -> str:
        """Block until the pending device-code login resolves (polling at the cadence
        Salesforce specified, honoring slow_down), persist the resulting token, and
        return it. This call blocks for the lifetime of the code — run it off the
        event loop rather than awaiting it directly.

        `_flow` deliberately stays set for the whole duration of this call (cleared
        only on the way out, success or failure) rather than the moment completion
        starts: a tool call landing on get_token() while this is in flight must see
        the SAME pending instructions and must not mint a second device code by
        calling _initiate() again — that would abandon the code this call is polling
        for and dead-end the user's first-run login."""
        with self._lock:
            if self._flow is None:
                raise LoginRequired(
                    "No device-code login is in progress — call get_token() first "
                    "to obtain sign-in instructions."
                )
            flow = self._flow

        try:
            body = self._poll_until_resolved(flow)
        except BaseException:
            with self._lock:
                self._flow = None
            raise

        with self._lock:
            self._flow = None

        if "access_token" not in body:
            detail = (
                body.get("error_description") or body.get("error") or "login did not complete"
            )
            raise LoginRequired(
                f"Device-code login failed: {detail}. Call get_token() again to retry."
            )

        self._store_tokens(body)
        return body["access_token"]

    def _poll_until_resolved(self, flow: dict) -> dict:
        interval = flow.get("interval", 5)
        deadline = monotonic() + self._poll_timeout_seconds
        consecutive_transient = 0
        while True:
            sleep(interval)
            body = _token_request(
                self._http,
                self._login_url,
                {"grant_type": "device", "client_id": self._client_id, "code": flow["device_code"]},
            )
            if "access_token" in body:
                return body
            error = body.get("error", "")
            if error == "slow_down":
                interval += 5
                consecutive_transient = 0
            elif error == "authorization_pending":
                consecutive_transient = 0
            elif error in _TERMINAL_POLL_ERRORS:
                # Genuine terminal OAuth outcomes per the doc's error table (see module
                # docstring): the user denied access, or the code is invalid/expired.
                return body
            else:
                # A network blip, a non-JSON maintenance page, a transient server_error,
                # or an undocumented code — _token_request folds all of these into an
                # {"error": ...} body. A single hiccup during what can be a minutes-long
                # wait must NOT abandon a device code the user may still be approving;
                # keep polling until the overall deadline or too many failures in a row.
                consecutive_transient += 1
                if consecutive_transient >= _MAX_TRANSIENT_POLLS:
                    return body
            if monotonic() > deadline:
                return {
                    "error": "timeout",
                    "error_description": "Device-code login timed out waiting for approval.",
                }

    def login_instructions(self) -> str | None:
        """Return the pending device-code instructions if a login is in progress,
        else None."""
        with self._lock:
            return self._flow["message"] if self._flow else None

    def instance_url(self) -> str:
        if self._instance_url is None:
            raise RuntimeError(
                "instance_url() was called before any get_token() succeeded — sign "
                "in first."
            )
        return self._instance_url

    def _refresh(self, refresh_token: str) -> str:
        body = _token_request(
            self._http,
            self._login_url,
            {
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "refresh_token": refresh_token,
            },
        )
        if "access_token" not in body:
            detail = body.get("error_description") or body.get("error") or "refresh failed"
            raise AuthError(f"Salesforce rejected the cached refresh token ({detail}).")
        self._store_tokens(body)
        return body["access_token"]

    def _store_tokens(self, body: dict) -> None:
        with self._lock:
            self._access_token = body["access_token"]
            # A refresh-token-grant response only includes a new refresh_token when
            # token rotation is enabled on the Connected App; when it's absent the
            # existing one is still good and must not be discarded.
            if "refresh_token" in body:
                self._refresh_token = body["refresh_token"]
            if "instance_url" in body:
                self._instance_url = body["instance_url"]
        self._persist_cache()

    def _load_cache(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            data = json.loads(self._cache_path.read_text())
        except (ValueError, json.JSONDecodeError, OSError) as exc:
            # A corrupt/truncated cache file (ValueError/JSONDecodeError) or one that
            # can't even be read (OSError — e.g. permissions changed out from under
            # it) must not brick startup — warn on stderr (naming the file so a
            # maintainer can inspect/delete it) and carry on with an empty cache; the
            # user just signs in again.
            print(
                f"sfdc-mcp-server: token cache at {self._cache_path} is corrupt "
                f"({exc}) — starting with an empty cache; you will need to sign in "
                "again.",
                file=sys.stderr,
            )
            return
        if not isinstance(data, dict):
            return
        self._access_token = data.get("access_token")
        self._refresh_token = data.get("refresh_token")
        self._instance_url = data.get("instance_url")

    def _persist_cache(self) -> None:
        with self._lock:
            payload = {
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
                "instance_url": self._instance_url,
            }
        self._cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Write to a temp file at the final 0o600 mode, then atomically rename over
        # the real path — a reader (or another process) never observes a
        # partially-written cache file, and the mode is never wider than 0o600 for
        # even an instant (os.replace inherits the temp file's mode, not the old
        # target's).
        tmp_path = self._cache_path.with_name(self._cache_path.name + ".tmp")
        fd = os.open(tmp_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle)
            os.replace(tmp_path, self._cache_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise


class ClientCredentialsAuth:
    """App-only auth via Salesforce's OAuth 2.0 client-credentials flow. Sees
    everything the Connected App's run-as user can see — no per-user ACLs beyond that
    one user's — so it's for service scenarios only (README carries this warning; it
    isn't enforced in code). The token is cached in memory only (never written to
    disk) and re-fetched whenever force_refresh=True."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        login_url: str,
        http: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._login_url = login_url.rstrip("/")
        self._http = (
            http
            if http is not None
            else httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=5.0))
        )
        self._access_token: str | None = None
        self._instance_url: str | None = None

    def get_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._access_token:
            return self._access_token

        body = _token_request(
            self._http,
            self._login_url,
            {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        if "access_token" not in body:
            error = body.get("error", "unknown_error")
            description = body.get("error_description", "")
            if error == "invalid_client":
                raise AuthError(
                    "Salesforce rejected the client credentials (invalid_client) — "
                    "verify SFDC_MCP_CLIENT_ID and SFDC_MCP_CLIENT_SECRET match a "
                    "current consumer key/secret on an External Client App (or "
                    "Connected App) with the client-credentials flow enabled and a "
                    "run-as user configured."
                )
            raise AuthError(
                f"Could not acquire a client-credentials token ({error}): "
                f"{description or 'no details returned by Salesforce'}. Verify the "
                "External Client App (or Connected App)'s client-credentials flow is "
                "enabled, and that SFDC_MCP_LOGIN_URL is a My Domain URL — "
                "login.salesforce.com and test.salesforce.com don't support this flow."
            )
        self._access_token = body["access_token"]
        if "instance_url" in body:
            self._instance_url = body["instance_url"]
        return self._access_token

    def instance_url(self) -> str:
        if self._instance_url is None:
            raise RuntimeError(
                "instance_url() was called before any get_token() succeeded — sign "
                "in first."
            )
        return self._instance_url


def build_auth(settings: Settings, http: httpx.Client | None = None) -> AuthProvider:
    """Construct the AuthProvider selected by SFDC_MCP_AUTH. Settings already
    validated the field matrix each mode needs (see settings.py), so this is a
    straight dispatch, not a second round of validation."""
    if settings.auth == "device_code":
        return DeviceCodeAuth(
            client_id=settings.client_id,
            login_url=settings.login_url,
            token_cache_path=settings.token_cache_path,
            http=http,
            timeout_seconds=settings.timeout_seconds,
        )
    return ClientCredentialsAuth(
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        login_url=settings.login_url,
        http=http,
        timeout_seconds=settings.timeout_seconds,
    )
