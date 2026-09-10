from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from github_workflows import cli, installation

ROOT = Path(__file__).parents[1]


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


@pytest.mark.parametrize(
    ("xdg_cache_home", "expected_cache", "expected_warning"),
    [
        (
            "relative-cache",
            ".cache",
            "[WARN] ignoring non-absolute XDG_CACHE_HOME; using ~/.cache\n",
        ),
        ("", ".cache", "[WARN] ignoring non-absolute XDG_CACHE_HOME; using ~/.cache\n"),
        ("~/skills-cache", "skills-cache", ""),
    ],
)
def test_xdg_cache_home_never_yields_relative_checkout(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_cache_home: str,
    expected_cache: str,
    expected_warning: str,
) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", xdg_cache_home)
    monkeypatch.chdir(cwd)

    installer = installation.Installer(arguments(), repository)

    assert installer.cache == home / expected_cache
    assert installer.cache.is_absolute()
    installer.plan_polars()
    installer.approve()

    assert "install the official Polars skill for Codex and Qwen" in installer.changes
    assert capsys.readouterr().err == expected_warning
    assert not (cwd / "~").exists()


def test_current_runtime_is_omitted_unless_verbose(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(installation, "command_path", lambda _name: "/bin/tool")
    executable_directory = repository / ".venv" / "bin"
    executable_directory.mkdir(parents=True)
    (executable_directory / "agent-workflows").touch()
    (executable_directory / "agent-feedback").touch()
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


def test_installer_manages_agent_feedback_command_link(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = repository / "home"
    executable = repository / ".venv" / "bin" / "agent-feedback"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setenv("PATH", str(home / ".local" / "bin"))
    installer = installation.Installer(arguments(), repository)
    installer.home = home

    installer.plan_agent_command()
    assert installer.changes == ["link the agent-feedback command"]
    assert installer.warnings == []

    installer.apply_agent_command()
    target = home / ".local" / "bin" / "agent-feedback"
    assert target.is_symlink()
    assert target.resolve() == executable.resolve()

    current = installation.Installer(arguments(verbose=True), repository)
    current.home = home
    current.plan_agent_command()
    assert current.changes == []
    assert "Agent feedback command is current" in capsys.readouterr().out


def test_installer_refuses_unmanaged_agent_feedback_command(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = repository / "home"
    target = home / ".local" / "bin" / "agent-feedback"
    target.parent.mkdir(parents=True)
    target.write_text("unmanaged\n")
    monkeypatch.setenv("PATH", str(target.parent))
    installer = installation.Installer(arguments(), repository)
    installer.home = home

    with pytest.raises(RuntimeError, match="refusing unmanaged agent-feedback command"):
        installer.plan_agent_command()

    assert installer.changes == []


def test_installer_warns_when_user_bin_is_not_on_path(repository: Path) -> None:
    installer = installation.Installer(arguments(), repository)
    installer.home = repository / "home"

    installer.plan_agent_command()

    assert any("is not on PATH" in warning for warning in installer.warnings)


def test_installer_rejects_when_another_agent_feedback_command_wins_path_lookup(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = repository / "home"
    shadow_directory = repository / "shadow-bin"
    shadow_directory.mkdir()
    shadow = shadow_directory / "agent-feedback"
    shadow.write_text("#!/bin/sh\n")
    shadow.chmod(0o755)
    managed_directory = home / ".local" / "bin"
    monkeypatch.setenv("PATH", f"{shadow_directory}{os.pathsep}{managed_directory}")
    installer = installation.Installer(arguments(), repository)
    installer.home = home

    with pytest.raises(RuntimeError, match=f"PATH resolves agent-feedback to {shadow}"):
        installer.plan_agent_command()


def test_command_link_is_listed_when_runtime_executable_is_missing(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installation, "command_path", lambda _name: "/bin/tool")
    installer = installation.Installer(arguments(), repository)
    installer.home = repository / "home"

    installer.plan_runtime()
    installer.plan_agent_command()

    assert installer.changes == [
        "install the Python environment",
        "link the agent-feedback command",
    ]


def test_excluding_runtime_skips_dependent_command_without_dropping_other_work(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    installer = installation.Installer(arguments(), repository)
    installer.add_change(
        "link the agent-feedback command",
        group="Shared",
        component="agent-command",
    )
    installer.add_change("link skill example", group="Codex", component="codex")

    assert installer.reconcile_selected_dependencies() is True

    assert installer.changes == ["link skill example"]
    assert installer.changed_components == {"codex"}
    assert "runtime installation was excluded" in capsys.readouterr().err


def test_command_link_skip_does_not_abort_when_runtime_source_disappears(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    installer = installation.Installer(arguments(), repository)
    installer.add_change(
        "link the agent-feedback command",
        group="Shared",
        component="agent-command",
    )

    installer.apply_agent_command()

    assert "skipped the agent-feedback command" in capsys.readouterr().err


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
    monkeypatch.setattr(installer, "plan_agent_command", lambda: None)
    monkeypatch.setattr(installer, "plan_codex", lambda: None)
    monkeypatch.setattr(installer, "plan_qwen", lambda: None)
    monkeypatch.setattr(installer, "plan_polars", lambda: None)
    monkeypatch.setattr(installer, "apply_runtime", lambda: applied.append("runtime"))
    monkeypatch.setattr(installer, "apply_agent_command", lambda: applied.append("agent-command"))
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


def test_data_confidentiality_rule_is_codex_only() -> None:
    rule = (
        "Treat a `data/` directory at the root of any repository as confidential. "
        "After detecting one, acknowledge once per conversation that its contents "
        "will remain unread. Never read, open, inspect, search within, summarize, "
        "print, copy, or modify file contents under it. List file and directory "
        "names only when needed to understand structure. If contents are required, "
        "request a sanitized sample outside `data/`. A repository may impose a "
        "stricter prohibition, including on listing names."
    )
    codex = (ROOT / "user-policies" / "codex.md").read_text(encoding="utf-8")
    qwen = (ROOT / "user-policies" / "qwen.md").read_text(encoding="utf-8")

    assert "## Confidential Data and Secrets" in codex
    assert rule in re.sub(r"\s+", " ", codex)
    assert "## Confidential Data and Secrets" not in qwen
    assert rule not in re.sub(r"\s+", " ", qwen)


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


@pytest.mark.parametrize("entrypoint", ["install", "workflow"])
def test_subprocess_failure_is_a_clean_cli_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    class Parser:
        @staticmethod
        def parse_args() -> argparse.Namespace:
            return argparse.Namespace(command=entrypoint)

    monkeypatch.setattr(cli, "build_parser", Parser)

    def fail(_args: argparse.Namespace) -> int:
        raise subprocess.CalledProcessError(
            1,
            ["uv", "pip", "install", "pkg"],
            output="partial stdout",
            stderr="simulated uv failure: network unreachable",
        )

    if entrypoint == "install":
        monkeypatch.setattr(cli, "install_from_args", fail)
    else:
        monkeypatch.setattr(cli, "run_workflow", fail)

    assert cli.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "uv pip install pkg" in captured.err
    assert "simulated uv failure: network unreachable" in captured.err
    assert "Traceback" not in captured.err


def test_subprocess_timeout_is_a_clean_cli_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Parser:
        @staticmethod
        def parse_args() -> argparse.Namespace:
            return argparse.Namespace(command="install")

    monkeypatch.setattr(cli, "build_parser", Parser)

    def stall(_args: argparse.Namespace) -> int:
        raise subprocess.TimeoutExpired(["uv", "venv", "/checkout/.venv"], 1800.0)

    monkeypatch.setattr(cli, "install_from_args", stall)

    assert cli.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "timed out" in captured.err
    assert "uv venv /checkout/.venv" in captured.err
    assert "Traceback" not in captured.err


def test_installer_run_binds_an_explicit_timeout(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(installation.subprocess, "run", fake_run)

    installer = installation.Installer(arguments(), repository)
    installer.run("echo", "hi")

    assert calls[0]["timeout"] == installation.INSTALL_STEP_TIMEOUT


def test_installer_run_timeout_raises_typed_error(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(installation.subprocess, "run", fake_run)

    installer = installation.Installer(arguments(), repository)
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        installer.run("uv", "venv", "/checkout/.venv")

    assert list(excinfo.value.cmd) == ["uv", "venv", "/checkout/.venv"]


def test_apply_runtime_pins_dev_hook_install_to_checkout_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "checkout"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
    (root / ".venv" / "bin" / "python").touch()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(installation, "command_path", lambda _name: "/bin/uv")

    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((tuple(command), dict(kwargs)))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(installation.subprocess, "run", fake_run)

    installer = installation.Installer(arguments(dev=True), root)
    installer.changes.append("install the Python environment")
    installer.apply_runtime()

    hook_calls = [
        kwargs
        for command, kwargs in calls
        if command[0] == str(root / ".venv" / "bin" / "pre-commit") and command[1:] == ("install",)
    ]
    assert len(hook_calls) == 1
    assert hook_calls[0]["cwd"] == installer.root
    state = json.loads((root / ".venv" / ".agent-workflows-install.json").read_text())
    assert state["dev"] is True


def test_apply_runtime_does_not_mark_runtime_current_when_hook_install_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "checkout"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
    (root / ".venv" / "bin" / "python").touch()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(installation, "command_path", lambda _name: "/bin/uv")

    hook_kwargs: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0].endswith("pre-commit") and command[1:] == ("install",):
            hook_kwargs.append(dict(kwargs))
            raise subprocess.CalledProcessError(
                1, command, output="", stderr="fatal: not a git repository"
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(installation.subprocess, "run", fake_run)

    installer = installation.Installer(arguments(dev=True), root)
    installer.changes.append("install the Python environment")
    with pytest.raises(subprocess.CalledProcessError):
        installer.apply_runtime()

    assert len(hook_kwargs) == 1
    assert hook_kwargs[0]["cwd"] == installer.root
    assert not (root / ".venv" / ".agent-workflows-install.json").exists()


def test_codex_mcp_inspection_timeout_degrades_to_skip_warning(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = installation.Installer(arguments(install_mcp=True), repository)
    installer.home = repository / "home"
    monkeypatch.setattr(
        installation, "command_path", lambda name: "/bin/codex" if name == "codex" else None
    )
    monkeypatch.delenv("QWEN_HOME", raising=False)
    seen_timeouts: list[object] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(installation.subprocess, "run", fake_run)

    installer.plan_mcp()

    assert seen_timeouts
    assert all(timeout == installation.MCP_INSPECTION_TIMEOUT for timeout in seen_timeouts)
    assert ("codex", "github") not in installer.mcp_changes
    assert ("codex", "context7") not in installer.mcp_changes
    assert any("could not inspect the Codex" in warning for warning in installer.warnings)


@pytest.mark.parametrize("old_exists", [True, False])
def test_moved_codex_skill_link_is_relinked(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    old_exists: bool,
) -> None:
    monkeypatch.setattr(installation, "command_path", lambda _name: "/bin/tool")
    home = repository / "home"
    new_source = repository / "codex" / "skills" / "gh-audit-repo"
    old_target = repository / "old-checkout" / "codex" / "skills" / "gh-audit-repo"
    if old_exists:
        old_target.mkdir(parents=True)
    link = home / ".codex" / "skills" / "gh-audit-repo"
    link.parent.mkdir(parents=True)
    link.symlink_to(old_target)

    installer = installation.Installer(arguments(), repository)
    installer.home = home
    installer.plan_codex()
    assert installer.changes == ["link skill gh-audit-repo"]
    assert installer.warnings == []
    installer.apply_codex()
    assert link.is_symlink()
    assert link.resolve() == new_source.resolve()

    again = installation.Installer(arguments(verbose=True), repository)
    again.home = home
    again.plan_codex()
    assert again.changes == []
    assert "Codex skill gh-audit-repo is current" in capsys.readouterr().out


def test_foreign_codex_skill_link_is_still_refused(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installation, "command_path", lambda _name: "/bin/tool")
    home = repository / "home"
    foreign = repository / "elsewhere" / "skills" / "gh-audit-repo"
    foreign.mkdir(parents=True)
    target = home / ".codex" / "skills" / "gh-audit-repo"
    target.parent.mkdir(parents=True)
    target.symlink_to(foreign)

    installer = installation.Installer(arguments(), repository)
    installer.home = home
    installer.plan_codex()

    assert installer.changes == []
    assert installer.warnings == [f"refusing unmanaged Codex skill: {target}"]


def test_moved_agent_feedback_link_is_relinked(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old_exec = repository / "old-checkout" / ".venv" / "bin" / "agent-feedback"
    old_exec.parent.mkdir(parents=True)
    old_exec.touch()
    new_source = repository / ".venv" / "bin" / "agent-feedback"
    new_source.parent.mkdir(parents=True)
    new_source.touch()
    home = repository / "home"
    link = home / ".local" / "bin" / "agent-feedback"
    link.parent.mkdir(parents=True)
    link.symlink_to(old_exec)
    monkeypatch.setenv("PATH", str(home / ".local" / "bin"))

    installer = installation.Installer(arguments(), repository)
    installer.home = home
    installer.plan_agent_command()  # must offer re-linking, not abort
    assert installer.changes == ["link the agent-feedback command"]
    assert installer.warnings == []
    installer.apply_agent_command()
    assert link.is_symlink()
    assert link.resolve() == new_source.resolve()

    again = installation.Installer(arguments(verbose=True), repository)
    again.home = home
    again.plan_agent_command()
    assert again.changes == []
    assert "Agent feedback command is current" in capsys.readouterr().out


def test_moved_qwen_extension_is_relinked(
    repository: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        installation, "command_path", lambda name: "/bin/qwen" if name == "qwen" else None
    )
    home = repository / "home"
    target = home / ".qwen" / "extensions" / "github-workflows"
    target.mkdir(parents=True)
    old_ext = repository / "old-checkout" / "extensions" / "github-workflows"
    old_ext.mkdir(parents=True)
    (target / ".qwen-extension-install.json").write_text(
        json.dumps({"type": "link", "source": str(old_ext)})
    )
    new_source = repository / "extensions" / "github-workflows"
    commands: list[tuple[str, ...]] = []

    def fake_run(*command: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    installer = installation.Installer(arguments(), repository)
    installer.home = home
    monkeypatch.setattr(installer, "run", fake_run)

    installer.plan_qwen()
    assert installer.changes == ["link the github-workflows extension"]
    installer.apply_qwen()
    assert commands == [
        ("/bin/qwen", "extensions", "uninstall", "github-workflows"),
        ("/bin/qwen", "extensions", "link", str(new_source)),
    ]

    (target / ".qwen-extension-install.json").write_text(
        json.dumps({"type": "link", "source": str(new_source)})
    )
    again = installation.Installer(arguments(verbose=True), repository)
    again.home = home
    again.plan_qwen()
    assert again.changes == []
    assert "Qwen extension is current" in capsys.readouterr().out


def test_moved_polars_links_are_reinstalled(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_source = (
        repository / "old-cache" / "agent-workflows" / "upstream" / "polars-skills" / "polars"
    )
    old_source.mkdir(parents=True)
    home = repository / "home"
    for agent in ("codex", "qwen"):
        target = home / f".{agent}" / "skills" / "polars"
        target.parent.mkdir(parents=True)
        target.symlink_to(old_source)
    cache = repository / "cache"
    new_source = cache / "agent-workflows" / "upstream" / "polars-skills" / "polars"
    new_source.mkdir(parents=True)

    installer = installation.Installer(arguments(), repository)
    installer.home = home
    installer.cache = cache

    def fake_run(*command: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(installer, "run", fake_run)

    installer.plan_polars()
    assert installer.changes == ["install the official Polars skill for Codex and Qwen"]
    installer.apply_polars()
    for agent in ("codex", "qwen"):
        target = home / f".{agent}" / "skills" / "polars"
        assert target.is_symlink()
        assert target.resolve() == new_source.resolve()
