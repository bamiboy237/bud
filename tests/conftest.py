import os

import pytest
from pydantic_ai import models

from bud.config import Settings, get_settings
from tests.offline import offline_requests

models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """Never load the developer's credentials, .env, or application data."""
    for name in tuple(os.environ):
        if name.lower() in Settings.model_fields:
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path.resolve() / "data"))
    # Never the developer's real ~/.bud/workspace.
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path.resolve() / "bud-home" / "workspace"))
    monkeypatch.setenv("APPROVAL_REVIEW", "false")
    monkeypatch.setenv("LOOP_DETECTION", "false")
    # Never the developer's real browser.
    monkeypatch.setenv("BROWSER_TOOLS", "false")
    monkeypatch.delenv("BUD_DAILY_USD", raising=False)
    for var in ("BUD_HOME", "BUD_HOST", "BUD_PORT", "BUD_BROWSE_HOME", "BUD_BROWSE_PORT"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    with offline_requests():
        yield
    get_settings.cache_clear()
