"""Thin httpx-based Salesforce REST API client: request/retry/paginate, nothing else.
Read-only by construction — `get` and `query_paged` are the only entry points anywhere
in this module. There is no `post`, `patch`, or `delete`; that asymmetry with a
typical API client is the whole safety story of this server, so it lives at the
client surface, not just in how the MCP tools happen to use it.

Every request goes through `_request`, which is the single place that: builds the
Bearer auth header from `auth.get_token()`, retries 429/503 honoring `Retry-After`
(falling back to exponential backoff), retries a 401 exactly once via
`auth.get_token(force_refresh=True)`, and reduces any failure to an `SfdcError` whose
`.message` is an actionable, credential-free sentence safe to return verbatim as an
MCP tool result — never a raw Salesforce error body or traceback, and never a token.

Two Salesforce-specific things bind the shape of this module:

- The instance URL (`auth.instance_url()`) can change after a token refresh — org
  migrations move a customer to a new instance and the *next* token response reflects
  it. So the base URL is never cached at construction; every request (including each
  hop of a `nextRecordsUrl` chain) re-reads `auth.instance_url()` fresh.
- Salesforce error bodies are JSON *arrays* of `{"message": ..., "errorCode": ...}` —
  the opposite shape from a success body, which may be a dict (a single record, a
  describe result, a query envelope) or, for some endpoints, a list. `get()` returns
  whatever shape Salesforce sent; only the non-JSON/3xx guard rejects a body outright.
"""

import re
from time import sleep

import httpx

from sfdc_mcp.auth import AuthProvider

# Retries after the initial attempt: 3 more tries (4 requests total) before giving up.
# Only 429 (throttled) and 503 (temporarily unavailable) are retried — Salesforce's
# other 5xx statuses (500, 502, 504) are usually a bad query or a Salesforce-side bug
# rather than a transient blip, so hammering them with retries is more likely to make
# things worse than better; those get a single actionable sentence instead.
MAX_RETRIES = 3
BACKOFF_SECONDS = (1, 2, 4)

_USAGE_RE = re.compile(r"(?<![-\w])api-usage=(\d+)/(\d+)")
_USAGE_WARN_THRESHOLD = 0.9


class SfdcError(Exception):
    """Raised for any Salesforce request that fails after the client's retry policy
    is exhausted. `.message` is an actionable, credential-free sentence; `.status` is
    the terminal HTTP status code, or None for a network-level failure (no response
    was ever received — DNS, connection refused, timeout)."""

    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _parse_retry_after(value: str | None) -> float | None:
    """Salesforce's 429/503 Retry-After is a delta-seconds integer in practice, not
    an HTTP-date. Fall back to the backoff sequence for anything we can't parse."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_sobject_path(path: str) -> tuple[str | None, str | None]:
    """Best-effort (sobject, record_id) from a `/sobjects/{Name}[/describe|/{id}]`
    path, e.g. '/sobjects/Account/001xx0000...' -> ('Account', '001xx0000...') and
    '/sobjects/Account/describe' or '/sobjects/Account' -> ('Account', None). The
    describe/bare-collection vs. record-id distinction matters for 404s: the former
    means the object name itself is wrong (or hidden), the latter means that specific
    record is missing (or ACL-hidden) — the two need different wording. Falls back to
    (None, None) for paths that don't follow this shape (e.g. '/query',
    '/parameterizedSearch', '/sobjects' itself)."""
    parts = [segment for segment in path.split("/") if segment]
    if "sobjects" not in parts:
        return None, None
    index = parts.index("sobjects")
    if index + 1 >= len(parts):
        return None, None
    sobject = parts[index + 1]
    if index + 2 < len(parts) and parts[index + 2] != "describe":
        return sobject, parts[index + 2]
    return sobject, None


def _fold_error_body(response: httpx.Response) -> tuple[str | None, str | None]:
    """Best-effort (message, errorCode) from a Salesforce JSON error body, which is
    an array of `{"message": ..., "errorCode": ...}` objects — fold the first entry,
    the same "first message wins" convention Salesforce's own tooling uses. Never
    raises — a non-JSON or unexpected-shape body just yields (None, None) and the
    caller falls back to a generic status-code message."""
    try:
        body = response.json()
    except ValueError:
        return None, None
    if isinstance(body, list) and body and isinstance(body[0], dict):
        entry = body[0]
        return entry.get("message"), entry.get("errorCode")
    if isinstance(body, dict):
        # Defensive: some Salesforce error responses (rare, non-REST-API surfaces)
        # come back as a single object rather than an array.
        return body.get("message"), body.get("errorCode")
    return None, None


def _non_json_message(status: int) -> str:
    """Shared wording for both the 3xx guard and a non-JSON 2xx body — the snow
    hibernation lesson: a wrong login URL, an org in maintenance, or an SSO front
    door in the way all tend to answer with HTML or a redirect instead of the
    Salesforce REST API's usual JSON envelope."""
    return (
        f"Salesforce returned a non-JSON response (HTTP {status}) — verify "
        "SFDC_MCP_LOGIN_URL points at the right org (a wrong login/My Domain URL "
        "commonly causes this), or this may be a Salesforce maintenance window; "
        "retry shortly."
    )


class SfdcClient:
    """Bearer-token Salesforce REST API client, per `AuthProvider`."""

    def __init__(
        self,
        auth: AuthProvider,
        api_version: str,
        http: httpx.Client | None = None,
        item_limit: int = 25,
        timeout_seconds: float = 30.0,
    ):
        self._auth = auth
        self._api_version = api_version
        self._item_limit = item_limit
        self._timeout_seconds = timeout_seconds
        self._http = (
            http
            if http is not None
            else httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=5.0))
        )
        # Most recent Sforce-Limit-Info reading, as (used, limit); None until a
        # response has carried a well-formed header.
        self._api_usage: tuple[int, int] | None = None

    def get(self, path: str, params: dict | None = None) -> dict | list:
        response = self._request(path, params=params)
        return self._parse_json_body(response)

    def query_paged(self, soql: str, limit: int | None = None) -> tuple[list[dict], int]:
        """GET /query?q=<soql>, following `nextRecordsUrl` (a full path already
        rooted at /services/data/vXX/... — called verbatim against the current
        instance_url, never re-prefixed with the version base again) until either
        `limit` records have been collected or Salesforce reports no further page.
        Returns (records capped at limit, totalSize) — totalSize is Salesforce's
        exact pre-pagination match count, always present on a /query response."""
        cap = limit if limit is not None else self._item_limit
        response = self._request("/query", params={"q": soql})
        body = self._require_dict(response)
        records = list(body.get("records", []))
        total = body.get("totalSize", 0)
        next_url = body.get("nextRecordsUrl")
        while next_url and len(records) < cap:
            response = self._request(next_url, absolute=True)
            body = self._require_dict(response)
            records.extend(body.get("records", []))
            next_url = body.get("nextRecordsUrl")
        return records[:cap], total

    def usage_note(self) -> str | None:
        """A one-line warning once the most recently observed Sforce-Limit-Info
        usage reaches 90% of the daily API request limit, else None."""
        if self._api_usage is None:
            return None
        used, limit = self._api_usage
        if limit <= 0 or used / limit < _USAGE_WARN_THRESHOLD:
            return None
        return f"Salesforce API usage: {used}/{limit} daily calls."

    def _require_dict(self, response: httpx.Response) -> dict:
        body = self._parse_json_body(response)
        if not isinstance(body, dict):
            raise SfdcError(
                response.status_code,
                "Salesforce returned an unexpected response shape for a SOQL query.",
            )
        return body

    def _base_url(self) -> str:
        # Re-read instance_url() on every call rather than caching it at
        # construction: it can change after a token refresh (an org migration
        # moves a customer to a new instance, reflected in the next token
        # response), so a stale cached value could silently start hitting the
        # wrong org.
        return f"{self._auth.instance_url()}/services/data/v{self._api_version}"

    def _resolve_url(self, path: str, *, absolute: bool) -> str:
        if absolute:
            # nextRecordsUrl is already a full path rooted at
            # /services/data/vXX/query/... — prefixing it with _base_url() again
            # would double up the version segment.
            return f"{self._auth.instance_url()}{path}"
        return f"{self._base_url()}/{path.lstrip('/')}"

    def _request(
        self, path: str, params: dict | None = None, *, absolute: bool = False
    ) -> httpx.Response:
        retries = 0
        refreshed = False
        while True:
            # get_token() FIRST, on every iteration: DeviceCodeAuth/ClientCredentialsAuth
            # only populate instance_url() as a side effect of a successful token
            # response, and instance_url() raises RuntimeError until that has happened
            # at least once. Resolving the URL before fetching a token would crash the
            # very first request in both auth modes (device_code: before any sign-in;
            # client_credentials: always, since it never gets a separate "first login"
            # moment) with a raw RuntimeError instead of LoginRequired/AuthError. This
            # also re-reads instance_url() fresh after every get_token() call — including
            # the retried request after a 401 forces a refresh below — so an org
            # migration's new instance_url is picked up immediately (see _base_url).
            token = self._auth.get_token()
            url = self._resolve_url(path, absolute=absolute)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            try:
                response = self._http.get(url, params=params, headers=headers)
            except httpx.TimeoutException as exc:
                # Distinct from "unreachable" — Salesforce was reachable but too
                # slow, so the message doesn't send someone chasing a network/DNS
                # problem that isn't there.
                raise SfdcError(
                    None,
                    f"Salesforce timed out after {self._timeout_seconds:.0f}s: {exc}",
                ) from exc
            except httpx.HTTPError as exc:
                # Never let the underlying exception (which may echo the request,
                # headers and all — including the Authorization bearer token) reach
                # the caller — reduce it to a plain, credential-free sentence.
                raise SfdcError(None, f"Salesforce unreachable: {exc}") from exc

            self._record_limit_info(response)

            if response.status_code in (429, 503) and retries < MAX_RETRIES:
                delay = _parse_retry_after(response.headers.get("Retry-After"))
                if delay is None:
                    delay = BACKOFF_SECONDS[retries]
                # Salesforce is trusted to send sane values, but a hostile or buggy
                # upstream sending an absurd (hours-long) or negative Retry-After
                # must not be able to hang the process or sleep(-N)-crash it.
                delay = max(0.0, min(delay, 60.0))
                retries += 1
                sleep(delay)
                continue

            if response.status_code == 401 and not refreshed:
                # One recovery attempt: the caller is telling us a token we
                # previously handed out was rejected, so force the provider to mint
                # (or refresh) a new one before trying again. If Salesforce still
                # says 401 after that, it's terminal — fall through to _error_for.
                refreshed = True
                self._auth.get_token(force_refresh=True)
                continue

            if 300 <= response.status_code < 400:
                # follow_redirects is off (httpx's default) by design — a redirect
                # means something other than the REST API answered, most commonly a
                # wrong SFDC_MCP_LOGIN_URL or a maintenance-window bounce. Neither is
                # safe to silently follow with a bearer token attached.
                raise SfdcError(response.status_code, _non_json_message(response.status_code))

            if response.status_code >= 400:
                raise self._error_for(response, path)

            return response

    def _record_limit_info(self, response: httpx.Response) -> None:
        """Parse `Sforce-Limit-Info: api-usage=1234/15000` off of every response,
        success or failure. A missing or malformed header is silently ignored —
        it's a courtesy header, not something Salesforce guarantees on every call."""
        header = response.headers.get("Sforce-Limit-Info")
        if not header:
            return
        match = _USAGE_RE.search(header)
        if not match:
            return
        used, limit = int(match.group(1)), int(match.group(2))
        self._api_usage = (used, limit)

    def _parse_json_body(self, response: httpx.Response) -> dict | list:
        try:
            body = response.json()
        except ValueError:
            raise SfdcError(response.status_code, _non_json_message(response.status_code)) from None
        if not isinstance(body, dict | list):
            raise SfdcError(response.status_code, _non_json_message(response.status_code))
        return body

    def _error_for(self, response: httpx.Response, path: str) -> SfdcError:
        status = response.status_code
        message, error_code = _fold_error_body(response)

        if status == 401:
            return SfdcError(
                401, "Salesforce rejected the request as unauthorized — sign in again."
            )

        if status == 403:
            if error_code == "REQUEST_LIMIT_EXCEEDED":
                return SfdcError(
                    403,
                    "Salesforce's daily API request limit is exhausted — it resets "
                    "over a 24-hour window; check Setup → System Overview.",
                )
            sobject, _ = _parse_sobject_path(path)
            if sobject:
                return SfdcError(
                    403,
                    f"Salesforce denied access to {sobject} (object- or "
                    "field-level security) — the signed-in user lacks permission.",
                )
            return SfdcError(
                403,
                "Salesforce denied access to this request (object- or "
                "field-level security).",
            )

        if status == 404:
            sobject, record_id = _parse_sobject_path(path)
            if sobject and record_id:
                return SfdcError(
                    404,
                    f"No record {record_id} found in {sobject} (or field-level "
                    "security hides it).",
                )
            if sobject:
                return SfdcError(
                    404,
                    f"Salesforce object {sobject} does not exist or isn't "
                    "accessible — check the name (list_sobjects can help).",
                )
            return SfdcError(404, "Salesforce returned 404 Not Found for this request.")

        if status == 400 and error_code == "MALFORMED_QUERY":
            return SfdcError(400, f"SOQL error: {message}" if message else "SOQL error.")

        if status in (500, 502, 504):
            return SfdcError(
                status,
                f"Salesforce returned a server error (HTTP {status}) — this is "
                "usually a problem with the query or request rather than a "
                "transient outage, so it was not retried; try again or simplify "
                "the request.",
            )

        if status in (429, 503):
            return SfdcError(
                status,
                f"Salesforce is still throttling requests after {MAX_RETRIES} "
                f"retries (HTTP {status}) — try again shortly.",
            )

        parts = [text for text in (message, error_code) if text]
        suffix = f": {' — '.join(parts)}" if parts else "."
        return SfdcError(status, f"Salesforce returned HTTP {status}{suffix}")
