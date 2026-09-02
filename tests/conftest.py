import pytest


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    """Never let a test write into the user's real RF Agent workspace.

    The settings singleton caches the resolved paths, so it has to be
    reset on both sides of the test.
    """
    from rf_agent import config

    monkeypatch.setenv("RF_AGENT_WORKSPACE", str(tmp_path / "ws"))
    config.reset_settings()
    yield
    config.reset_settings()
