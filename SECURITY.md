# Security Policy

## Supported versions

sfdc-mcp-server is pre-1.0. Only the latest commit on `main` and the most recent
tagged release are supported with security fixes. There is no long-term-support
branch.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub Security Advisories: open the
repo's **Security** tab and use **"Report a vulnerability"**. Do not open a
public issue for anything that could be exploited before a fix ships.

Include what you'd include in a bug report — affected version/commit,
reproduction steps, and impact. We'll acknowledge new reports within a few
business days and follow up with a plan or fix timeline.

## Scope

sfdc-mcp-server is a local stdio MCP server: it runs on your machine, launched
by your MCP client, and talks only to the Salesforce REST API. There is no
network listener and no multi-user surface — the deployment environment (the
machine it runs on, and the MCP client that launches it) is yours to secure.

Tokens live on disk: the device-code token cache at
`~/.sfdc-mcp/token_cache.json` (mode `600`) holds a refresh token, and
`SFDC_MCP_CLIENT_SECRET` belongs in environment variables or a secrets
manager — never commit either. Anyone with read access to that cache file or
your environment effectively holds your Salesforce access; treat local file
permissions and the client secret accordingly.

The server is read-only by construction — its HTTP client exposes only `GET`,
and `soql_query` additionally rejects any query that isn't a `SELECT`. That
`client_credentials` (unattended) mode sees everything the Connected/External
Client App's configured run-as user can see, with no per-caller access
control, is a documented property of the mode — see the README's auth-mode
warning — not a vulnerability.
