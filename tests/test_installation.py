from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from github_workflows import cli, installation


def arguments(**overrides: object) -> argparse.Namespace:
    values = {
        "dev": False,
        "dry_run": True,
        "yes": False,
        "verbose": False,
        "machine_role": "local",
        "install_mcp": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
    (tmp_path / "user-policies").mkdir()
    (tmp_path / "user-policies" / "codex.md").write_text("Codex policy\n")
    (tmp_path / "user-policies" / "qwen.md").write_text("Qwen policy\n")
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
    assert "install Codex user instructions" in output
    assert "install Qwen user instructions" in output
    assert "Codex:\n    1) install Codex user instructions" in output
    assert "link skill gh-audit-repo" in output
    assert "Qwen:" in output
    assert "Shared:" in output
    assert "install the official Polars skill for Codex and Qwen" in output
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


def test_install_applies_only_changed_components(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installation, "command_path", lambda _name: "/bin/tool")
    installer = installation.Installer(arguments(dry_run=False, yes=True), repository)
    installer.home = repository / "home"
    installer.cache = repository / "cache"
    applied: list[str] = []
    monkeypatch.setattr(installer, "plan_runtime", lambda: None)
    monkeypatch.setattr(installer, "plan_codex", lambda: None)
    monkeypatch.setattr(installer, "plan_qwen", lambda: None)
    monkeypatch.setattr(installer, "plan_polars", lambda: None)
    monkeypatch.setattr(installer, "apply_runtime", lambda: applied.append("runtime"))
    monkeypatch.setattr(installer, "apply_codex", lambda: applied.append("codex"))
    monkeypatch.setattr(installer, "apply_qwen", lambda: applied.append("qwen"))
    monkeypatch.setattr(installer, "apply_polars", lambda: applied.append("polars"))
    assert installer.install() == 0
    assert applied == []
    assert (installer.home / ".codex" / "AGENTS.md").is_file()
    assert (installer.home / ".qwen" / "QWEN.md").is_file()


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


def test_matching_user_policy_files_are_adopted_as_managed_files(
    repository: Path,
) -> None:
    installer = installation.Installer(arguments(), repository)
    installer.home = repository / "home"
    codex_target = installer.home / ".codex" / "AGENTS.md"
    qwen_target = installer.home / ".qwen" / "QWEN.md"
    codex_target.parent.mkdir(parents=True)
    qwen_target.parent.mkdir(parents=True)
    codex_target.write_bytes((repository / "user-policies" / "codex.md").read_bytes())
    qwen_target.write_bytes((repository / "user-policies" / "qwen.md").read_bytes())

    installer.plan_user_policies()
    installer.apply_user_policies()

    assert installer.changes == [
        "install Codex user instructions",
        "install Qwen user instructions",
    ]
    assert not codex_target.is_symlink()
    assert not qwen_target.is_symlink()
    assert codex_target.read_text().startswith(installation.USER_POLICY_MARKER)
    assert qwen_target.read_text().startswith(installation.USER_POLICY_MARKER)


def test_remote_compute_policy_is_only_rendered_for_remote_role(repository: Path) -> None:
    remote_policy = repository / "user-policies" / "remote-compute.md"
    remote_policy.write_text("## Remote only\n\nSlurm policy\n")

    local = installation.Installer(arguments(), repository)
    remote = installation.Installer(arguments(machine_role="remote"), repository)

    local_policy = local.rendered_user_policy(repository / "user-policies" / "codex.md")
    remote_policy_text = remote.rendered_user_policy(repository / "user-policies" / "codex.md")
    assert "Slurm policy" not in local_policy
    assert remote_policy_text.endswith("## Remote only\n\nSlurm policy\n")


def test_unmanaged_user_policy_is_never_replaced(repository: Path) -> None:
    installer = installation.Installer(arguments(), repository)
    installer.home = repository / "home"
    target = installer.home / ".codex" / "AGENTS.md"
    target.parent.mkdir(parents=True)
    target.write_text("local policy\n")

    installer.plan_user_policies()
    installer.apply_user_policies()

    assert target.read_text() == "local policy\n"
    assert installer.warnings == [f"refusing unmanaged Codex user instructions: {target}"]


def test_noninteractive_install_requires_yes(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = installation.Installer(arguments(dry_run=False), repository)
    installer.changes.append("change")
    monkeypatch.setattr(installation.sys.stdin, "isatty", lambda: False)

    with pytest.raises(RuntimeError, match="rerun with --yes"):
        installer.approve()


def test_mcp_plan_offers_each_missing_server_for_each_client(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = installation.Installer(arguments(install_mcp=True), repository)
    installer.home = repository / "home"
    monkeypatch.setattr(installation, "command_path", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        installer,
        "_inspect_mcp",
        lambda client, _executable: {"github"} if client == "codex" else set(),
    )

    installer.plan_mcp()

    assert installer.changes == [
        "configure the context7 MCP for Codex",
        "configure the github MCP for Qwen",
        "configure the context7 MCP for Qwen",
    ]
    assert installer.mcp_changes == {
        ("codex", "context7"),
        ("qwen", "github"),
        ("qwen", "context7"),
    }


def test_qwen_mcp_inspection_reads_names_without_connecting(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = installation.Installer(arguments(install_mcp=True), repository)
    installer.home = repository / "home"
    monkeypatch.delenv("QWEN_HOME", raising=False)
    settings = installer.home / ".qwen" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {"httpUrl": "https://custom.example/mcp"},
                    "unrelated": {"command": "server"},
                }
            }
        )
    )

    assert installer._inspect_mcp("qwen", "/bin/qwen") == {"github"}


def test_qwen_mcp_inspection_honors_qwen_home(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = installation.Installer(arguments(install_mcp=True), repository)
    installer.home = repository / "unused-home"
    qwen_home = tmp_path / "custom-qwen"
    qwen_home.mkdir()
    (qwen_home / "settings.json").write_text(
        json.dumps({"mcpServers": {"context7": {"httpUrl": "https://custom.example"}}})
    )
    monkeypatch.setenv("QWEN_HOME", str(qwen_home))

    assert installer._inspect_mcp("qwen", "/bin/qwen") == {"context7"}


def test_qwen_mcp_inspection_accepts_comments_without_altering_strings(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = installation.Installer(arguments(install_mcp=True), repository)
    installer.home = repository / "home"
    monkeypatch.delenv("QWEN_HOME", raising=False)
    settings = installer.home / ".qwen" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        """{
          // Qwen accepts comments in its settings.
          "mcpServers": {
            "github": {"httpUrl": "https://custom.example/mcp"},
            /* Keep this user-owned server unchanged. */
            "context7": {"httpUrl": "https://docs.example/mcp"}
          }
        }
        """
    )

    assert installer._inspect_mcp("qwen", "/bin/qwen") == {"github", "context7"}


def test_codex_mcp_inspection_preserves_configured_names(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = installation.Installer(arguments(install_mcp=True), repository)

    def inspect(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        name = command[3]
        return subprocess.CompletedProcess(
            command,
            0 if name == "github" else 1,
            stdout="{}" if name == "github" else "",
            stderr="" if name == "github" else "No MCP server named 'context7' found.",
        )

    monkeypatch.setattr(installation.subprocess, "run", inspect)

    assert installer._inspect_mcp("codex", "/bin/codex") == {"github"}


def test_install_arguments_offer_mcp_by_default() -> None:
    parser = argparse.ArgumentParser()
    installation.add_install_arguments(parser)

    assert parser.parse_args([]).install_mcp is True
    assert parser.parse_args(["--skip-mcp"]).install_mcp is False


def test_apply_mcp_uses_native_user_level_commands(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installer = installation.Installer(arguments(install_mcp=True), repository)
    for client, name in (("codex", "github"), ("qwen", "context7")):
        label = client.capitalize()
        installer.add_change(f"configure the {name} MCP for {label}", group=label, component="mcp")
        installer.mcp_changes.add((client, name))
    monkeypatch.setattr(installation, "command_path", lambda name: f"/bin/{name}")
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        installer,
        "run",
        lambda *command, **_kwargs: commands.append(command),
    )

    installer.apply_mcp()

    assert commands == [
        (
            "/bin/codex",
            "mcp",
            "add",
            "github",
            "--url",
            installation.MCP_SERVERS["github"],
        ),
        (
            "/bin/qwen",
            "mcp",
            "add",
            "--scope",
            "user",
            "--transport",
            "http",
            "context7",
            installation.MCP_SERVERS["context7"],
        ),
    ]
    assert capsys.readouterr().out.splitlines() == [
        "[APPLY] Codex: configure the github MCP for Codex",
        "[APPLY] Qwen: configure the context7 MCP for Qwen",
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", set()),
        ("N", set()),
        ("A", {1, 2, 3, 4, 5}),
        ("1,3-4", {1, 3, 4}),
        ("2 5", {2, 5}),
    ],
)
def test_yay_style_exclusions(value: str, expected: set[int]) -> None:
    assert installation.Installer.parse_exclusions(value, 5) == expected


@pytest.mark.parametrize("value", ["0", "6", "3-2", "1-x", "1 ^3"])
def test_yay_style_exclusions_reject_invalid_choices(value: str) -> None:
    with pytest.raises(ValueError, match="selection"):
        installation.Installer.parse_exclusions(value, 5)


def test_interactive_selection_groups_clients_and_excludes_by_number(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installer = installation.Installer(arguments(dry_run=False), repository)
    installer.add_change("install Codex workflow", group="Codex", component="codex")
    installer.add_change("install Qwen workflow", group="Qwen", component="qwen")
    installer.add_change("install runtime", group="Shared", component="runtime")
    monkeypatch.setattr(installation.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "2")

    assert installer.approve() is True

    assert installer.changes == ["install Codex workflow", "install runtime"]
    assert installer.changed_components == {"codex", "runtime"}
    output = capsys.readouterr().out
    assert "Codex:\n    1) install Codex workflow" in output
    assert "Qwen:\n    2) install Qwen workflow" in output
    assert "Shared:\n    3) install runtime" in output


def test_interactive_selection_can_exclude_everything(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installer = installation.Installer(arguments(dry_run=False), repository)
    installer.add_change("install Codex workflow", group="Codex", component="codex")
    monkeypatch.setattr(installation.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "A")

    assert installer.approve() is False
    assert installer.changes == []
    assert installer.changed_components == set()
    assert "[OK] Nothing selected" in capsys.readouterr().out


def test_keyboard_interrupt_is_a_silent_cli_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Parser:
        @staticmethod
        def parse_args() -> argparse.Namespace:
            return argparse.Namespace(command="install")

    monkeypatch.setattr(cli, "build_parser", Parser)

    def interrupt(_args: argparse.Namespace) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "install_from_args", interrupt)

    assert cli.main() == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
