"""Bud's workspace lives in one home folder, the same for every account binding."""

from bud.config import Settings
from bud.core.workspace_home import display_path, workspace_home


def test_a_fresh_install_gets_a_private_home(tmp_path):
    home = workspace_home(Settings(workspace_dir=tmp_path / ".bud" / "workspace"))
    assert home.is_dir()
    assert oct(home.stat().st_mode & 0o777) == "0o700"
    assert workspace_home(Settings(workspace_dir=tmp_path / ".bud" / "workspace")) == home


def test_the_default_home_is_under_the_owners_home_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("WORKSPACE_DIR")
    monkeypatch.setenv("HOME", str(tmp_path))
    home = workspace_home(Settings())
    assert home == tmp_path.resolve() / ".bud" / "workspace"
    assert display_path(home) == "~/.bud/workspace"


def test_an_empty_workspace_setting_means_the_default_not_the_current_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WORKSPACE_DIR", "")
    assert workspace_home(Settings()) == tmp_path.resolve() / ".bud" / "workspace"
