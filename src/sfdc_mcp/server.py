"""FastMCP server exposing five read-only Salesforce tools over stdio (plan.md's Tools
table). Tools are thin: parse args, call the injected SfdcClient, format compact text —
no raw Salesforce JSON ever reaches an MCP client. Every tool catches `LoginRequired`
(returns the device-code sign-in instructions as the tool result, and kicks off a
background thread to finish the login so the *next* call has a chance of succeeding
without a second manual round-trip), `AuthError`, and `SfdcError` (returns its
actionable message) rather than letting any of the three propagate as a traceback.
Every non-empty tool result carries `client.usage_note()` as a trailing line once
Salesforce's daily API usage crosses 90% (see client.py).

Tests swap in a MockTransport-backed SfdcClient (and a FakeAuth) via `configure()` —
the module-level `_state` it sets is what every tool function reads through
`_get_state()`, so nothing here talks to a real SfdcClient/AuthProvider directly. This
mirrors m365-mcp-server's server.py shape verbatim, including the async-offload
pattern: every registered tool is `async def` and awaits its sync `_*_sync` body via
`anyio.to_thread.run_sync` so a slow Salesforce request never blocks the event loop.

Full-text search binds to GET /parameterizedSearch (q=<term> + repeated sobject=<name>
scoping params) — NOT raw SOSL string interpolation, so there's no FIND-clause
injection surface. The GET parameter names below were verified against Salesforce's
REST API docs (2026-07-12) rather than recalled from memory:
https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_search_parameterized_get.htm
"""

import re
import sys
import threading
from functools import partial
from urllib.parse import quote

import anyio.to_thread
from mcp.server.fastmcp import FastMCP

from sfdc_mcp.auth import AuthError, AuthProvider, LoginRequired, build_auth
from sfdc_mcp.client import SfdcClient, SfdcError
from sfdc_mcp.settings import Settings

mcp = FastMCP("salesforce")

# describe_sobject's field-list cap — orgs (especially ones with many managed
# packages) can have several hundred fields on a single sobject; capping keeps the
# tool result a useful skim rather than a multi-thousand-line wall of text.
_DESCRIBE_FIELD_CAP = 200

# Picklist values shown per field before truncating with "…" — enough to recognize the
# picklist's shape without reproducing a 200-value list inline.
_PICKLIST_VALUE_CAP = 10

_SOQL_REJECTION = (
    "Only SELECT queries are accepted — this server is read-only by construction. "
    "Try something like: SELECT Id, Name FROM Account WHERE CreatedDate = "
    "LAST_N_DAYS:7 LIMIT 10."
)


def _q(value: str) -> str:
    """Percent-encode an sobject name or record id for safe interpolation into a
    Salesforce REST URL path segment — mirrors m365-mcp-server's `_q` helper."""
    return quote(value, safe="")


def _split_csv(value: str) -> list[str]:
    """Comma-separated list -> stripped, non-empty entries. "" -> []."""
    return [item.strip() for item in value.split(",") if item.strip()]


# -- SELECT-only guard ----------------------------------------------------------------


def _strip_leading_noise(soql: str) -> str:
    """Strip leading whitespace and SOQL comments (`//` line comments, `/* */` block
    comments), repeated, so `_first_keyword` sees the real first token even through
    something like `/* why */ // also\n  SELECT ...`. An unterminated comment (a
    dangling `/*` with no matching `*/`) consumes the rest of the string, which is the
    conservative choice — the query is malformed either way and must not be mistaken
    for starting with SELECT."""
    text = soql
    while True:
        stripped = text.lstrip()
        if stripped.startswith("//"):
            newline = stripped.find("\n")
            stripped = stripped[newline + 1 :] if newline != -1 else ""
        elif stripped.startswith("/*"):
            end = stripped.find("*/")
            stripped = stripped[end + 2 :] if end != -1 else ""
        if stripped == text:
            return stripped
        text = stripped


def _first_keyword(soql: str) -> str | None:
    text = _strip_leading_noise(soql)
    match = re.match(r"[A-Za-z]+", text)
    return match.group(0).upper() if match else None


def _reject_non_select(soql: str) -> str | None:
    """None when `soql`'s first keyword (after stripping leading whitespace/comments)
    is SELECT, else the friendly rejection sentence. Checked before any network call —
    the belt-and-braces half of this server's read-only story (the other half is that
    SfdcClient exposes no write endpoints at all)."""
    if _first_keyword(soql) == "SELECT":
        return None
    return _SOQL_REJECTION


# -- record id shape validation ---------------------------------------------------------


def _valid_record_id(record_id: str) -> bool:
    """Salesforce record ids are 15 (case-sensitive) or 18 (case-insensitive, with a
    3-character checksum suffix) base-62 [0-9A-Za-z] characters — never anything a
    plain `.isalnum()` would admit from outside ASCII."""
    return len(record_id) in (15, 18) and record_id.isascii() and record_id.isalnum()


# -- record formatting: Id first, then Name, then the rest in response order ----------


def _format_field(key: str, value: object) -> list[str]:
    if key == "attributes":
        return []
    if isinstance(value, dict):
        # A relationship field (e.g. Account: {"attributes": ..., "Name": "Acme"}) —
        # rendered one level deep as dotted paths (Account.Name: Acme), its own nested
        # "attributes" skipped the same as the top level's.
        return [
            f"{key}.{sub_key}: {sub_value}"
            for sub_key, sub_value in value.items()
            if sub_key != "attributes"
        ]
    return [f"{key}: {value}"]


def _format_record(record: dict) -> str:
    ordered_keys: list[str] = []
    if "Id" in record:
        ordered_keys.append("Id")
    if "Name" in record:
        ordered_keys.append("Name")
    for key in record:
        if key == "attributes" or key in ordered_keys:
            continue
        ordered_keys.append(key)
    lines: list[str] = []
    for key in ordered_keys:
        lines.extend(_format_field(key, record[key]))
    return "\n".join(lines)


class _State:
    """Bundles the SfdcClient tools call with the AuthProvider that backs it (needed
    for the LoginRequired background-completion kick-off) and the configured
    item_limit (list_sobjects' cap is a multiple of it; SfdcClient keeps its own copy
    privately, so the server tracks its own rather than reaching into the client)."""

    def __init__(self, client: SfdcClient, auth: AuthProvider, item_limit: int):
        self.client = client
        self.auth = auth
        self.item_limit = item_limit


_state: _State | None = None
_completion_lock = threading.Lock()
_completion_thread: threading.Thread | None = None


def configure(client: SfdcClient, auth: AuthProvider, item_limit: int = 25) -> None:
    """Inject the SfdcClient/AuthProvider pair the tools operate on. Called once by
    `main()` at startup, and by tests to swap in fakes."""
    global _state
    _state = _State(client, auth, item_limit)


def _get_state() -> _State:
    if _state is None:
        raise RuntimeError("sfdc_mcp.server.configure() must be called before any tool runs.")
    return _state


def _spawn_completion(auth: AuthProvider) -> None:
    """On LoginRequired, kick off the device-code completion in the background so the
    *next* tool call has a chance of succeeding without the user having to trigger a
    second round-trip manually. Guarded so it's a no-op for providers without
    `complete_login` (e.g. ClientCredentialsAuth never raises LoginRequired anyway, but
    this stays defensive) and race-safe against concurrent tool calls each hitting
    LoginRequired at once — only one completion thread ever runs at a time."""
    complete_login = getattr(auth, "complete_login", None)
    if complete_login is None:
        return
    global _completion_thread
    with _completion_lock:
        if _completion_thread is not None and _completion_thread.is_alive():
            return
        thread = threading.Thread(target=_run_completion, args=(complete_login,), daemon=True)
        _completion_thread = thread
        thread.start()


def _run_completion(complete_login) -> None:
    # Best-effort: nothing consumes the result here. On failure (code expired, denied)
    # the next foreground get_token() call raises a fresh LoginRequired anyway, so
    # there's nothing useful to do with the exception besides not letting it kill the
    # (daemon) thread noisily.
    try:
        complete_login()
    except Exception as exc:  # noqa: BLE001
        print(f"sfdc-mcp-server: device-code login did not complete: {exc}", file=sys.stderr)


def _handle(exc: LoginRequired | AuthError | SfdcError) -> str:
    if isinstance(exc, LoginRequired):
        _spawn_completion(_get_state().auth)
    return str(exc)


def _finish(text: str) -> str:
    """Append client.usage_note() as a trailing line when Salesforce's daily API usage
    has crossed 90% (see client.py) — every tool's final formatting step."""
    note = _get_state().client.usage_note()
    if note is None:
        return text
    return f"{text}\n\n{note}"


# -- soql_query -------------------------------------------------------------------------


def _soql_query_sync(query: str, limit: int = 25) -> str:
    rejection = _reject_non_select(query)
    if rejection:
        return rejection

    try:
        records, total = _get_state().client.query_paged(query, limit)
    except (LoginRequired, AuthError, SfdcError) as exc:
        return _handle(exc)

    if not records:
        return _finish("No records matched this query.")

    segments = [_format_record(record) for record in records]
    shown = len(records)
    # totalSize can exceed the shown count even when Salesforce reports done=true (a
    # LIMIT clause caps records returned but not totalSize) — "showing N of M reported
    # matches" says exactly that without implying "M more available", which would be
    # wrong for a LIMIT-capped query.
    if total > shown:
        segments.append(f"(showing {shown} of {total} reported matches)")
    return _finish("\n\n".join(segments))


@mcp.tool()
async def soql_query(query: str, limit: int = 25) -> str:
    """Run a read-only SOQL SELECT query against Salesforce, returning up to `limit`
    matching records (default 25). Only SELECT statements are accepted — this server
    is read-only by construction; anything else gets a friendly rejection instead of
    reaching Salesforce. One line of SOQL to get you started:
    SELECT Id, Name FROM Account WHERE CreatedDate = LAST_N_DAYS:7 LIMIT 10
    """
    return await anyio.to_thread.run_sync(partial(_soql_query_sync, query, limit))


# -- get_record -------------------------------------------------------------------------


def _get_record_sync(sobject: str, record_id: str, fields: str = "") -> str:
    if not _valid_record_id(record_id):
        return (
            f"{record_id!r} doesn't look like a Salesforce record Id — expected 15 "
            "or 18 alphanumeric characters. Check the value and try again."
        )

    params = {"fields": fields} if fields else None
    try:
        record = _get_state().client.get(f"/sobjects/{_q(sobject)}/{_q(record_id)}", params=params)
    except (LoginRequired, AuthError, SfdcError) as exc:
        return _handle(exc)

    if not isinstance(record, dict):
        return "Salesforce returned an unexpected response shape for this record."
    return _finish(_format_record(record))


@mcp.tool()
async def get_record(sobject: str, record_id: str, fields: str = "") -> str:
    """Fetch one record by Id from `sobject` (e.g. Account, Contact, or a custom
    object like MyObject__c). `fields` is an optional comma-separated list of field
    names to return (default: every field Salesforce returns for the object). To find
    records first: soql_query("SELECT Id, Name FROM Account LIMIT 10")."""
    return await anyio.to_thread.run_sync(
        partial(_get_record_sync, sobject, record_id, fields)
    )


# -- search -----------------------------------------------------------------------------


def _search_sync(term: str, sobjects: str = "", limit: int = 25) -> str:
    params: dict[str, object] = {"q": term, "overallLimit": limit}
    scoped = _split_csv(sobjects)
    if scoped:
        params["sobject"] = scoped

    try:
        result = _get_state().client.get("/parameterizedSearch", params=params)
    except (LoginRequired, AuthError, SfdcError) as exc:
        return _handle(exc)

    if not isinstance(result, dict):
        return "Salesforce returned an unexpected response shape for this search."

    hits = (result.get("searchRecords") or [])[:limit]
    if not hits:
        return _finish(f"No results for {term!r}.")

    segments = []
    for hit in hits:
        obj_type = (hit.get("attributes") or {}).get("type", "Record")
        segments.append(f"[{obj_type}]\n{_format_record(hit)}")
    return _finish("\n\n".join(segments))


@mcp.tool()
async def search(term: str, sobjects: str = "", limit: int = 25) -> str:
    """Full-text search Salesforce for `term` (name/email/phone fields by default),
    optionally scoped to a comma-separated list of sobject names in `sobjects` (e.g.
    "Account,Contact"). Returns up to `limit` hits (default 25), each labeled with its
    object type. For structured filters use soql_query instead, e.g.
    soql_query("SELECT Id, Name FROM Contact WHERE Email LIKE '%@acme.com'")."""
    return await anyio.to_thread.run_sync(partial(_search_sync, term, sobjects, limit))


# -- describe_sobject ---------------------------------------------------------------------


def _format_describe_field(field: dict) -> str:
    name = field.get("name", "?")
    ftype = field.get("type", "?")
    label = field.get("label", "")
    line = f"{name} ({ftype}, {label})"
    if ftype in ("picklist", "multipicklist"):
        values = [
            entry.get("value")
            for entry in field.get("picklistValues") or []
            if entry.get("active", True)
        ]
        if values:
            shown = values[:_PICKLIST_VALUE_CAP]
            suffix = ", …" if len(values) > _PICKLIST_VALUE_CAP else ""
            line += f" [{', '.join(shown)}{suffix}]"
    return line


def _describe_sobject_sync(sobject: str) -> str:
    try:
        result = _get_state().client.get(f"/sobjects/{_q(sobject)}/describe")
    except (LoginRequired, AuthError, SfdcError) as exc:
        return _handle(exc)

    if not isinstance(result, dict):
        return "Salesforce returned an unexpected response shape for this object's schema."

    fields = result.get("fields") or []
    shown_fields = fields[:_DESCRIBE_FIELD_CAP]
    lines = [_format_describe_field(f) for f in shown_fields]
    if len(fields) > _DESCRIBE_FIELD_CAP:
        lines.append(
            f"... {len(fields) - _DESCRIBE_FIELD_CAP} more fields not shown "
            f"(capped at {_DESCRIBE_FIELD_CAP})."
        )

    header = f"{result.get('label', sobject)} ({sobject})"
    return _finish(f"{header}\n" + "\n".join(lines))


@mcp.tool()
async def describe_sobject(sobject: str) -> str:
    """Describe an sobject's schema: every field's name, type, and label, with up to
    10 picklist values shown per picklist field. Useful before writing a SOQL query
    against an object you haven't queried before — e.g. describe_sobject("Account")
    then SELECT Id, Name FROM Account WHERE Industry = 'Technology' LIMIT 10."""
    return await anyio.to_thread.run_sync(partial(_describe_sobject_sync, sobject))


# -- list_sobjects ------------------------------------------------------------------------


def _list_sobjects_sync() -> str:
    try:
        result = _get_state().client.get("/sobjects")
    except (LoginRequired, AuthError, SfdcError) as exc:
        return _handle(exc)

    if not isinstance(result, dict):
        return "Salesforce returned an unexpected response shape for the object list."

    sobjects = result.get("sobjects") or []
    queryable = [entry for entry in sobjects if entry.get("queryable")]
    # Orgs commonly have 1000+ sobjects (standard + every managed package's custom
    # objects) — item_limit*10 keeps the listing usable while still being generous
    # relative to the other tools' per-call cap.
    cap = _get_state().item_limit * 10
    shown = queryable[:cap]
    if not shown:
        return _finish("No queryable Salesforce objects were found.")

    lines = [f"{entry.get('name', '?')} — {entry.get('label', '')}" for entry in shown]
    if len(queryable) > cap:
        lines.append(
            f"... {len(queryable) - cap} more queryable objects not shown (capped at "
            f"{cap} — orgs commonly have 1000+ sobjects)."
        )
    return _finish("\n".join(lines))


@mcp.tool()
async def list_sobjects() -> str:
    """List every queryable Salesforce object (standard and custom) available to the
    signed-in identity, as `name — label` pairs — a starting point for soql_query
    (e.g. soql_query("SELECT Id, Name FROM <name> LIMIT 5")) and describe_sobject."""
    return await anyio.to_thread.run_sync(_list_sobjects_sync)


def main() -> None:
    """Console-script entry point: build settings/auth/client from the environment and
    run the MCP server over stdio (the standard transport for local MCP servers)."""
    try:
        settings = Settings()
        auth = build_auth(settings)
    except ValueError as exc:
        # Settings()'s and build_auth's ValueError messages are already actionable
        # sentences naming the env var to fix — surface exactly that on stderr, not a
        # traceback, and exit non-zero so a process supervisor sees a clean failure.
        print(f"sfdc-mcp-server: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    client = SfdcClient(
        auth,
        api_version=settings.api_version,
        item_limit=settings.item_limit,
        timeout_seconds=settings.timeout_seconds,
    )
    configure(client, auth, settings.item_limit)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
