"""Runtime config resolution across developer checkouts and standalone ~/.bud installs."""

from pathlib import Path

from bud.config import get_settings, resolve_runtime_paths


def test_resolve_runtime_paths_prefers_explicit_bud_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    dot_bud = home / ".bud"
    dot_bud.mkdir(parents=True)
    (dot_bud / "config.env").write_text("DEEPSEEK_API_KEY=dot\n")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("DEEPSEEK_API_KEY=repo\n")

    custom = tmp_path / "custom-bud"
    custom.mkdir()
    (custom / "config.env").write_text("DEEPSEEK_API_KEY=custom\n")

    monkeypatch.setenv("HOME", str(home))
    env_file, data_dir, workspace_dir = resolve_runtime_paths(
        cwd=repo, env={"BUD_HOME": str(custom)}
    )
    assert env_file == custom / "config.env"
    assert data_dir == custom / "data"
    assert workspace_dir == custom / "workspace"


def test_resolve_runtime_paths_uses_checkout_before_user_bud_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    dot_bud = home / ".bud"
    dot_bud.mkdir(parents=True)
    (dot_bud / "config.env").write_text("DEEPSEEK_API_KEY=dot\n")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("DEEPSEEK_API_KEY=repo\n")

    empty = tmp_path / "outside"
    empty.mkdir()

    monkeypatch.setenv("HOME", str(home))
    repo_paths = resolve_runtime_paths(cwd=repo, env={})
    assert repo_paths == (repo / ".env", Path("data"), None)

    home_paths = resolve_runtime_paths(cwd=empty, env={})
    assert home_paths == (dot_bud / "config.env", dot_bud / "data", dot_bud / "workspace")


def test_get_settings_applies_bud_home_and_respects_explicit_data_dir(tmp_path, monkeypatch):
    bud_home = tmp_path / "prod-bud"
    bud_home.mkdir()
    (bud_home / "config.env").write_text("BUD_PORT=9123\nOPENAI_API_KEY=sk-test\n")

    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_DIR", raising=False)
    monkeypatch.setenv("BUD_HOME", str(bud_home))
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.data_dir == bud_home / "data"
    assert settings.workspace_dir == bud_home / "workspace"
    assert settings.port == 9123

    override = tmp_path / "explicit-data"
    monkeypatch.setenv("DATA_DIR", str(override))
    get_settings.cache_clear()
    overridden = get_settings()
    assert overridden.data_dir == override
    assert overridden.workspace_dir == bud_home / "workspace"
