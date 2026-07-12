# Manual verification (live org)

CI runs the full suite against a faked Salesforce REST API and never touches a real
org. Before a release, a maintainer runs this script once against a real Salesforce
org to verify the end-to-end path CI cannot: real device-code login, real SOQL
results, real schema.

Prerequisites: a free [Developer Edition
org](https://developer.salesforce.com/signup) (no credit card required) you can sign
in to, an External Client App (or classic Connected App, if your org still allows
creating one) set up for **device_code** per the README's walkthrough (device flow
enabled, `api` + `refresh_token` scopes, "Require Secret for Refresh Token Flow"
left unchecked), and at least one Account record in the org (a fresh Developer
Edition org ships with a handful of sample Accounts already).

1. Set up the app per the README (or reuse an existing registration) and start the
   server from this checkout with a clean cache, so the login path is actually
   exercised:

       rm -f ~/.sfdc-mcp/token_cache.json
       export SFDC_MCP_CLIENT_ID=<consumer-key>
       uv run sfdc-mcp-server

   Then connect an MCP client to it — easiest is `claude mcp add sfdc-dev -e
   SFDC_MCP_CLIENT_ID=<consumer-key> -- uv run --directory <this-checkout>
   sfdc-mcp-server` and a fresh `claude` session.

2. Call `soql_query` with `SELECT Id, Name FROM Account LIMIT 5`. **Expected:** the
   result is the device-code sign-in instructions (a verification URL and a short
   code) as the tool's first-call UX, not an error or a stack trace. Open the URL,
   enter the code, and complete the login in a browser.

3. Call `soql_query` with the same query again. **Expected:** real Account records
   (Id first, then Name), with no re-prompt for login — the background completion
   thread finished the login after step 2. Note one of the returned Ids.

4. Call `get_record` with `sobject="Account"` and the Id from step 3, unmodified.
   **Expected:** that record's full field list, Id and Name first — this confirms
   the ids in tool output compose into follow-up calls without any client-side
   editing.

5. Call `search` with a word from a known Account's name (`term=<word>`,
   `sobjects="Account"`). **Expected:** the known Account among the hits, each
   labeled `[Account]` (or whatever object type matched).

6. Call `describe_sobject` with `sobject="Account"`. **Expected:** a field list
   (name, type, label) with `Id` and `Name` present; any picklist field (e.g.
   `Industry`) shows up to 10 values in brackets.

7. Call `list_sobjects`. **Expected:** a `name — label` listing that includes
   `Account` and `Contact` among the queryable objects.

8. Bad-auth check: stop the server, set `SFDC_MCP_CLIENT_ID` to a garbage value,
   restart, and call `soql_query` again. **Expected:** an actionable sentence
   naming `SFDC_MCP_CLIENT_ID` (device-code initiation failing cleanly), not a
   traceback. Restore the real client ID afterward.

9. Sign-off note on throttling and limits: 429s and the daily
   `REQUEST_LIMIT_EXCEEDED` 403 can't be forced on demand against a live org (a
   fresh Developer Edition org's daily limit is generous), so the retry path
   (sleep per `Retry-After`, max 3 retries, exponential 1→2→4s backoff without the
   header) and the limit-exhausted message are verified by the unit suite
   (`tests/test_client.py`) rather than this script. If Salesforce's
   `Sforce-Limit-Info` usage crosses 90% during steps 2–7, the observable behavior
   is a trailing `Salesforce API usage: N/M daily calls.` line on the tool
   result — seeing it is a pass, a missing or malformed line is a fail.

Record the date, org, and outcome of a run in the release PR description.
