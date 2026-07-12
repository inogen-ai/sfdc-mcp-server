import pytest


@pytest.fixture
def anyio_backend():
    # Only asyncio is a project dependency (trio isn't installed) — pin the anyio
    # pytest plugin's backend parametrization to the one we actually run under.
    return "asyncio"
