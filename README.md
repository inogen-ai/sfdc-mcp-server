# sfdc-mcp-server

[![CI](https://img.shields.io/github/actions/workflow/status/inogen-ai/sfdc-mcp-server/ci.yml?branch=main&label=CI)](https://github.com/inogen-ai/sfdc-mcp-server/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

![read-only Salesforce tools driven over MCP](docs/assets/demo.gif)

A production-grade, **read-only** MCP server for Salesforce: SOQL queries, record
reads, full-text search, and schema discovery through the Salesforce REST API, from
Claude Desktop, Claude Code, or any MCP client — with auth done properly (OAuth 2.0
device-code and client-credentials flows) and API-limit awareness (Salesforce's daily
request-limit usage is surfaced, and throttling is retried honoring `Retry-After`),
which the existing servers in this space skip.

**Read-only by construction, not by convention.** The HTTP client this server is built
on exposes only `GET` and `POST` — there is no write call to the Salesforce **data**
API — the only POST anywhere is the OAuth token exchange, which creates no records — so
no tool here could write to Salesforce even by accident. SOQL queries are additionally
guarded: `soql_query` rejects anything whose first keyword isn't `SELECT` before a
request is ever sent.

sfdc-mcp-server is not affiliated with, endorsed by, or sponsored by Salesforce, Inc.

## Quickstart

Requires a Salesforce Connected App or External Client App (see below) and
[uv](https://docs.astral.sh/uv/). Don't have a Salesforce org to test against? Sign up
for a free [Developer Edition org](https://developer.salesforce.com/signup) — no
credit card required.

    uvx sfdc-mcp-server

That starts the server over stdio. In practice you'll point an MCP client at it instead
of running it directly — for Claude Code:

    claude mcp add salesforce -e SFDC_MCP_CLIENT_ID=<your-consumer-key> -- uvx sfdc-mcp-server

For Claude Desktop, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "salesforce": {
      "command": "uvx",
      "args": ["sfdc-mcp-server"],
      "env": {
        "SFDC_MCP_CLIENT_ID": "<your-consumer-key>"
      }
    }
  }
}
```

The first tool call you make (e.g. `soql_query`) won't have a cached token yet — it
returns device-code sign-in instructions (a URL and a short code) as the tool result
instead of data. Open the URL, enter the code, sign in. The server finishes the login
in the background as soon as you do, so the next tool call succeeds without any extra
steps on your end.

## Connected App setup

Salesforce's Spring '26 release disabled creating new classic Connected Apps in most
orgs — **use an External Client App instead** (Salesforce's own successor to Connected
Apps; existing Connected Apps still work, but a fresh Developer Edition org signed up
today can't create a new one through the UI). The steps below use External Client Apps;
if your org still has classic Connected App creation enabled, the same settings exist
under **Setup → App Manager → New Connected App** instead.

Both auth modes start the same way: **Setup** → Quick Find → **External Client App
Manager** → **New External Client App** → give it a name, API name, and contact email,
then in the app's OAuth Settings check **Enable OAuth**.

### Device flow (interactive — for a human signing in)

1. Under OAuth Settings, check **Enable for Device Flow**. If a Callback URL field is
   required to save the form, any placeholder works
   (`https://login.salesforce.com/services/oauth2/success` is a common one) — it's
   never used by the device flow, which has no browser redirect.
2. Under **OAuth Scopes**, add `api` (data access) and `refresh_token` (its alias
   `offline_access` works too) — move both to **Selected OAuth Scopes**. Without
   `refresh_token`, the server can't renew a session silently and you'll be asked to
   sign in again every couple of hours.
3. **Important:** leave **Require Secret for Refresh Token Flow** *unchecked*. Device
   flow is a public client — it has no secret to send — and checking this box makes
   Salesforce reject the server's silent refresh-token renewal with `invalid_client`,
   forcing a fresh interactive login every time the access token expires.
4. Save. On the app's detail page, reveal and copy the **Consumer Key** — this is
   `SFDC_MCP_CLIENT_ID`. Device flow is a public client, so there's no secret to copy
   and `SFDC_MCP_CLIENT_SECRET` stays unset.
5. Set `SFDC_MCP_AUTH=device_code` (the default — this variable can be omitted) and
   `SFDC_MCP_CLIENT_ID=<the consumer key from step 4>`.

### Client credentials (unattended — for service/automation use)

**Read the warning under [Auth modes](#auth-modes) before using this mode** — it sees
everything the run-as user is granted, with no per-caller access control.

1. Under OAuth Settings, check **Enable Client Credentials Flow** and accept the
   security-risk warning. Under **OAuth Scopes**, add `api` and move it to **Selected
   OAuth Scopes**.
2. Save, then from the app's **Manage** page → **Edit Policies** → under **Client
   Credentials Flow**, set **Run As** to an integration user with exactly the object-
   and field-level permissions you want this server to have — see the warning above.
3. On the app's detail page, reveal and copy the **Consumer Key** and **Consumer
   Secret** — these are `SFDC_MCP_CLIENT_ID` and `SFDC_MCP_CLIENT_SECRET`.
4. Client credentials flow requires a **My Domain** login URL — `login.salesforce.com`
   and `test.salesforce.com` aren't accepted for this flow. Find yours under **Setup →
   My Domain** (it looks like `https://your-domain.my.salesforce.com`); set
   `SFDC_MCP_LOGIN_URL` to it.
5. Set `SFDC_MCP_AUTH=client_credentials`, `SFDC_MCP_CLIENT_ID`,
   `SFDC_MCP_CLIENT_SECRET`, and `SFDC_MCP_LOGIN_URL` from the steps above.

## Tools

| Tool | Parameters | Returns |
|---|---|---|
| `soql_query` | `query: str`, `limit: int = 25` | Up to `limit` records from a read-only SOQL `SELECT`. Anything else (`UPDATE`, `DELETE`, leading comments hiding a non-SELECT statement, etc.) is rejected before any request is sent. |
| `get_record` | `sobject: str`, `record_id: str`, `fields: str = ""` | One record by Id, optionally limited to a comma-separated `fields` list. |
| `search` | `term: str`, `sobjects: str = ""`, `limit: int = 25` | Cross-object full-text search (name/email/phone fields), optionally scoped to a comma-separated `sobjects` list. |
| `describe_sobject` | `sobject: str` | Every field's name, type, and label, with up to 10 picklist values shown per picklist field. |
| `list_sobjects` | *(none)* | Every queryable object (standard and custom) the signed-in identity can see, as `name — label` pairs. |

One line of SOQL to get you started: `SELECT Id, Name FROM Account WHERE CreatedDate =
LAST_N_DAYS:7 LIMIT 10`. `list_sobjects` and `describe_sobject` are good starting
points for exploring an org you haven't queried before.

## Auth modes

|  | `device_code` (interactive) | `client_credentials` (unattended) |
|---|---|---|
| Who signs in | A human, via browser device-code login | Nobody — the Connected/External Client App itself |
| Effective access | Whatever the signed-in user can see — object/field-level security and sharing rules respected by construction | **Whatever the run-as user configured on the app can see, with no per-caller access control — the server reads what that one identity reads, regardless of who's asking through the MCP client** |
| Required env vars | `SFDC_MCP_CLIENT_ID` | `SFDC_MCP_CLIENT_ID`, `SFDC_MCP_CLIENT_SECRET`, `SFDC_MCP_LOGIN_URL` (a My Domain URL) |
| Use for | Interactive use — Claude Desktop, Claude Code, a human at a terminal | Service/automation scenarios only, where that run-as user's fixed, broad view of the org is an accepted tradeoff |

**`client_credentials` bypasses per-caller access control entirely — every tool call
sees exactly what the run-as user configured on the app can see, whoever is actually
asking through the MCP client. Scope that user's permission set as narrowly as the
integration allows, and use this mode only for service scenarios, never as a
convenience shortcut for interactive use.**

## Environment variables

All settings are prefixed `SFDC_MCP_` and can be set in the environment or a `.env`
file (see `.env.example`).

| Variable | Default | Purpose |
|---|---|---|
| `SFDC_MCP_AUTH` | `device_code` | Auth mode: `device_code` or `client_credentials`. |
| `SFDC_MCP_LOGIN_URL` | `https://login.salesforce.com` | OAuth login endpoint. Sandboxes use `https://test.salesforce.com`; `client_credentials` requires a My Domain URL instead of either. |
| `SFDC_MCP_CLIENT_ID` | *(unset, required)* | Connected/External Client App's consumer key. |
| `SFDC_MCP_CLIENT_SECRET` | *(unset)* | Consumer secret; required for `client_credentials` only. |
| `SFDC_MCP_API_VERSION` | `62.0` | Salesforce REST API version. |
| `SFDC_MCP_ITEM_LIMIT` | `25` | Default max records/results returned per call before "showing N of M" is reported. |
| `SFDC_MCP_TIMEOUT_SECONDS` | `30.0` | HTTP timeout (seconds) per Salesforce request. |
| `SFDC_MCP_TOKEN_CACHE_PATH` | `~/.sfdc-mcp/token_cache.json` | Where the device-code token cache is persisted (mode `600`). |

The Salesforce instance URL is never a setting here — it comes back from the OAuth
token response (`instance_url`) once signed in.

## API limits

Salesforce enforces a per-org daily API request allocation. This server tracks the
`Sforce-Limit-Info` header off every response and appends a usage note to a tool's
result once daily usage crosses 90% (e.g. `Salesforce API usage: 14500/15000 daily
calls.`), so a client sees the ceiling coming rather than hitting it mid-session. A
`429`/`503` is retried honoring `Retry-After` (clamped to at most 60s, up to 3
retries, exponential 1→2→4s backoff otherwise); a `403` with
`REQUEST_LIMIT_EXCEEDED` (the daily allocation already exhausted) returns an
actionable sentence about the 24-hour reset window, not a stack trace.

## Security notes

- **Read-only by construction.** The HTTP client exposes `GET` and `POST`, but there's
  no write call to the Salesforce **data** API for a tool to call even by mistake — the
  only POST anywhere is the OAuth token exchange (`/services/oauth2/token`), which
  creates no records. `soql_query` additionally rejects any query whose first keyword
  isn't `SELECT`.
- **Token cache on disk.** The device-code token cache (it holds a refresh token, not
  just an access token) is written to `~/.sfdc-mcp/token_cache.json` at mode `600`,
  created with that mode rather than chmod'd after the fact.
- **API-limit awareness.** See [API limits](#api-limits) — usage is surfaced before
  the daily ceiling is hit, never as a stack trace.
- Not affiliated with, endorsed by, or sponsored by Salesforce, Inc.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

    uv sync
    uv run pytest -q
    uv run ruff check .

No live Salesforce org is needed for the test suite — the REST API is faked at the
`httpx.MockTransport` boundary for unit tests, and a real stdio round-trip runs against
an in-process fake Salesforce API in `tests/integration/`. See
[docs/manual-verification.md](docs/manual-verification.md) for the live-org check a
maintainer runs before releases.

## Contributing

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and PR
expectations, and [SECURITY.md](SECURITY.md) for reporting vulnerabilities privately.

---

Not affiliated with, endorsed by, or sponsored by Salesforce, Inc.

Part of [InoGen's open-source portfolio](https://github.com/inogen-ai): [kilnworks](https://github.com/inogen-ai/kilnworks) (self-hostable RAG assistant) and the read-only MCP connectors [m365](https://github.com/inogen-ai/m365-mcp-server), [servicenow](https://github.com/inogen-ai/snow-mcp-server), [salesforce](https://github.com/inogen-ai/sfdc-mcp-server), and [hubspot](https://github.com/inogen-ai/hubspot-mcp-server).

Built and maintained by [InoGen](https://inogen.ai).
