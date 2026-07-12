# Contributing to sfdc-mcp-server

## Dev setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

    uv sync
    uv run pytest -q
    uv run ruff check .

No Salesforce org or credentials are needed: the unit suite fakes the REST API
at the `httpx.MockTransport` boundary, and `tests/integration/` runs a real
stdio round-trip against an in-process fake Salesforce API on a background
thread. The live-org check maintainers run before releases is documented in
[docs/manual-verification.md](docs/manual-verification.md) — you don't need to
run it for a PR.

## Pull requests

- Add tests for any behavior change. Bug fixes should include a test that fails
  without the fix.
- `uv run pytest -q` and `uv run ruff check .` must be clean; CI runs both on
  Python 3.11/3.12/3.13.
- Keep PRs small and focused on one change — easier to review, easier to revert.
- Never introduce a write path: v0.1 is read-only by construction (the HTTP
  client exposes only `GET`, and `soql_query` rejects non-`SELECT` statements),
  and PRs adding write capability will be declined until the project takes
  that step deliberately.
- Update the README when behavior, env vars, or tool signatures change.
- Describe *why*, not just *what*, in the PR description.

## Discussion

Use GitHub issues for bugs, feature proposals, and questions. There's no
separate mailing list or chat — keep the discussion where the code is.
