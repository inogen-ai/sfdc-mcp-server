# Every Settings() call in this module passes _env_file=None: these tests validate an
# exact env-var matrix via monkeypatch, and a developer's own .env file sitting in the
# repo root (pydantic-settings' default env_file=".env") could otherwise inject a
# stray SFDC_MCP_* value and flip an assertion — the same class of leak the
# integration gate's env-scrub guards against (see tests/integration/_stdio_entry.py).

import pytest

from sfdc_mcp.settings import Settings


def _device_env(monkeypatch, client_id="3MVG9c1id"):
    monkeypatch.setenv("SFDC_MCP_CLIENT_ID", client_id)


def _cc_env(monkeypatch, client_id="3MVG9c1id", client_secret="s3cret"):
    monkeypatch.setenv("SFDC_MCP_AUTH", "client_credentials")
    monkeypatch.setenv("SFDC_MCP_CLIENT_ID", client_id)
    monkeypatch.setenv("SFDC_MCP_CLIENT_SECRET", client_secret)


# -- defaults / round trip -----------------------------------------------------------


def test_defaults(monkeypatch):
    _device_env(monkeypatch)
    settings = Settings(_env_file=None)

    assert settings.auth == "device_code"
    assert settings.login_url == "https://login.salesforce.com"
    assert settings.client_id == "3MVG9c1id"
    assert settings.client_secret == ""
    assert settings.api_version == "62.0"
    assert settings.item_limit == 25
    assert settings.timeout_seconds == 30.0
    assert settings.token_cache_path == "~/.sfdc-mcp/token_cache.json"


def test_env_round_trip(monkeypatch):
    _cc_env(monkeypatch)
    monkeypatch.setenv("SFDC_MCP_LOGIN_URL", "https://acme.my.salesforce.com")
    monkeypatch.setenv("SFDC_MCP_API_VERSION", "60.0")
    monkeypatch.setenv("SFDC_MCP_ITEM_LIMIT", "10")
    monkeypatch.setenv("SFDC_MCP_TIMEOUT_SECONDS", "5.5")
    monkeypatch.setenv("SFDC_MCP_TOKEN_CACHE_PATH", "/tmp/cache.json")

    settings = Settings(_env_file=None)

    assert settings.auth == "client_credentials"
    assert settings.login_url == "https://acme.my.salesforce.com"
    assert settings.client_id == "3MVG9c1id"
    assert settings.client_secret == "s3cret"
    assert settings.api_version == "60.0"
    assert settings.item_limit == 10
    assert settings.timeout_seconds == 5.5
    assert settings.token_cache_path == "/tmp/cache.json"


def test_unprefixed_env_vars_are_ignored(monkeypatch):
    _device_env(monkeypatch)
    monkeypatch.setenv("CLIENT_ID", "should-not-be-picked-up")
    monkeypatch.setenv("AUTH", "client_credentials")

    settings = Settings(_env_file=None)

    assert settings.client_id == "3MVG9c1id"
    assert settings.auth == "device_code"


# -- auth validation matrix -----------------------------------------------------------


def test_unknown_auth_mode_raises(monkeypatch):
    _device_env(monkeypatch)
    monkeypatch.setenv("SFDC_MCP_AUTH", "oauth2")

    with pytest.raises(ValueError, match="SFDC_MCP_AUTH"):
        Settings(_env_file=None)


def test_device_code_requires_client_id(monkeypatch):
    with pytest.raises(ValueError, match="SFDC_MCP_CLIENT_ID"):
        Settings(_env_file=None)


def test_client_credentials_requires_client_id(monkeypatch):
    monkeypatch.setenv("SFDC_MCP_AUTH", "client_credentials")
    monkeypatch.setenv("SFDC_MCP_CLIENT_SECRET", "s3cret")

    with pytest.raises(ValueError, match="SFDC_MCP_CLIENT_ID"):
        Settings(_env_file=None)


def test_client_credentials_requires_client_secret(monkeypatch):
    monkeypatch.setenv("SFDC_MCP_AUTH", "client_credentials")
    monkeypatch.setenv("SFDC_MCP_CLIENT_ID", "3MVG9c1id")

    with pytest.raises(ValueError, match="SFDC_MCP_CLIENT_SECRET"):
        Settings(_env_file=None)


def test_device_code_does_not_require_client_secret(monkeypatch):
    _device_env(monkeypatch)

    settings = Settings(_env_file=None)

    assert settings.client_secret == ""


# -- login_url validation matrix -------------------------------------------------------


def test_login_url_http_scheme_raises(monkeypatch):
    _device_env(monkeypatch)
    monkeypatch.setenv("SFDC_MCP_LOGIN_URL", "http://login.salesforce.com")

    with pytest.raises(ValueError, match="https"):
        Settings(_env_file=None)


def test_login_url_http_localhost_allowed(monkeypatch):
    _device_env(monkeypatch)
    monkeypatch.setenv("SFDC_MCP_LOGIN_URL", "http://localhost:8080")

    settings = Settings(_env_file=None)

    assert settings.login_url == "http://localhost:8080"


def test_login_url_http_127_allowed(monkeypatch):
    _device_env(monkeypatch)
    monkeypatch.setenv("SFDC_MCP_LOGIN_URL", "http://127.0.0.1:8080")

    settings = Settings(_env_file=None)

    assert settings.login_url == "http://127.0.0.1:8080"


def test_login_url_trailing_slash_and_path_stripped(monkeypatch):
    _device_env(monkeypatch)
    monkeypatch.setenv("SFDC_MCP_LOGIN_URL", "https://login.salesforce.com/some/path")

    settings = Settings(_env_file=None)

    assert settings.login_url == "https://login.salesforce.com"


def test_login_url_not_a_url_raises(monkeypatch):
    _device_env(monkeypatch)
    monkeypatch.setenv("SFDC_MCP_LOGIN_URL", "not-a-url")

    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_test_salesforce_sandbox_url_allowed(monkeypatch):
    _device_env(monkeypatch)
    monkeypatch.setenv("SFDC_MCP_LOGIN_URL", "https://test.salesforce.com")

    settings = Settings(_env_file=None)

    assert settings.login_url == "https://test.salesforce.com"
