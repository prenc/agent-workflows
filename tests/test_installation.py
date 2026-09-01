from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pytest

from github_workflows import installation


def arguments(**overrides: bool) -> argparse.Namespace:
    values = {"dev": False, "dry_run": True, "yes": False, "verbose": False}
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
    (tmp_path / "codex" / "skills" / "gh-audit-repo").mkdir(parents=True)
    (tmp_path / "extensions" / "github-workflows").mkdir(parents=True)
    return tmp_path


def test_dry_run_lists_only_required_changes(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(installation, "command_path", lambda name: f"/bin/{name}")
    installer = installation.Installer(arguments(), repository)
    installer.home = repository / "home"
    installer.cache = repository / "cache"

    assert installer.install() == 0

    output = capsys.readouterr().out
    assert "install the Python environment" in output
    assert "link Codex skill gh-audit-repo" in output
    assert "clone the official Polars skill" in output
    assert "Installation complete" not in output


def test_current_runtime_is_omitted_unless_verbose(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(installation, "command_path", lambda _name: "/bin/tool")
    executable = repository / ".venv" / "bin" / "agent-workflows"
    executable.parent.mkdir(parents=True)
    executable.touch()
    state = {
        "dev": False,
        "pyproject_sha256": hashlib.sha256(
            (repository / "pyproject.toml").read_bytes()
        ).hexdigest(),
    }
    (repository / ".venv" / ".agent-workflows-install.json").write_text(json.dumps(state))
    installer = installation.Installer(arguments(), repository)
    installer.plan_runtime()
    assert capsys.readouterr().out == ""

    verbose = installation.Installer(arguments(verbose=True), repository)
    verbose.plan_runtime()
    assert "Python environment is current" in capsys.readouterr().out


def test_unmanaged_codex_skill_is_never_replaced(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installation, "command_path", lambda _name: "/bin/tool")
    installer = installation.Installer(arguments(), repository)
    installer.home = repository / "home"
    target = installer.home / ".codex" / "skills" / "gh-audit-repo"
    target.mkdir(parents=True)

    installer.plan_codex()

    assert not installer.changes
    assert installer.warnings == [f"refusing unmanaged Codex skill: {target}"]


def test_noninteractive_install_requires_yes(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = installation.Installer(arguments(dry_run=False), repository)
    installer.changes.append("change")
    monkeypatch.setattr(installation.sys.stdin, "isatty", lambda: False)

    with pytest.raises(RuntimeError, match="rerun with --yes"):
        installer.approve()
