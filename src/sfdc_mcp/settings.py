"""Runtime configuration for sfdc-mcp-server, loaded from SFDC_MCP_-prefixed
environment variables (or a .env file). `Settings()` validates two things eagerly, at
construction time, so a misconfigured server fails fast at startup with an actionable
message rather than failing obscurely on the first tool call: the auth-mode field
matrix (device_code needs a client ID; client_credentials needs a client ID *and* a
client secret) and that `login_url` is a plausible https Salesforce login endpoint
(localhost is allowed too, for pointing at a local test server).

The Salesforce instance URL is deliberately not a setting here — it comes back from
the OAuth token response (`instance_url`) and is exposed by the auth provider once
signed in (see auth.py), never configured by hand.
"""

from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SFDC_MCP_", env_file=".env")

    auth: str = "device_code"
    login_url: str = "https://login.salesforce.com"
    client_id: str = ""
    client_secret: str = ""
    api_version: str = "62.0"
    item_limit: int = 25
    timeout_seconds: float = 30.0
    token_cache_path: str = "~/.sfdc-mcp/token_cache.json"

    @model_validator(mode="after")
    def _validate_auth(self) -> "Settings":
        if self.auth not in ("device_code", "client_credentials"):
            raise ValueError(
                f"SFDC_MCP_AUTH={self.auth!r} is not a supported auth mode — set it "
                "to 'device_code' or 'client_credentials'."
            )
        if not self.client_id:
            raise ValueError(
                "SFDC_MCP_CLIENT_ID is required — set it to your Salesforce External "
                "Client App (or Connected App)'s consumer key."
            )
        if self.auth == "client_credentials" and not self.client_secret:
            raise ValueError(
                "SFDC_MCP_CLIENT_SECRET is required for client_credentials auth — "
                "set it to the External Client App (or Connected App)'s consumer "
                "secret (device_code auth is a public client and doesn't use one)."
            )
        return self

    @model_validator(mode="after")
    def _validate_login_url(self) -> "Settings":
        parts = urlsplit(self.login_url)
        if not parts.netloc:
            raise ValueError(f"SFDC_MCP_LOGIN_URL is not a valid URL: {self.login_url!r}")
        if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError(
                f"SFDC_MCP_LOGIN_URL must be https (got {self.login_url!r}) — "
                "Salesforce's OAuth endpoints are always https; http is allowed only "
                "for localhost, to point at a local test server."
            )
        # Normalize away any trailing slash/path so downstream URL-joining in auth.py
        # never produces a doubled slash.
        self.login_url = f"{parts.scheme}://{parts.netloc}"
        return self
