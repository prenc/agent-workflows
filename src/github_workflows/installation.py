"""Install the repository's user policies, Qwen extension, and Codex skills."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

POLARS_URL = "https://github.com/polars-inc/skills"
USER_POLICY_MARKER = "<!-- managed by agent-workflows; edit user-policies sources -->"
MCP_SERVERS = {
    "github": "https://api.githubcopilot.com/mcp/",
    "context7": "https://mcp.context7.com/mcp",
}


def strip_json_comments(value: str) -> str:
    """Remove JavaScript comments while preserving strings and line numbers."""
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(value):
        character = value[index]
        following = value[index + 1] if index + 1 < len(value) else ""
        if in_string:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            output.append(character)
            index += 1
            continue
        if character == "/" and following == "/":
            output.extend("  ")
            index += 2
            while index < len(value) and value[index] not in "\r\n":
                output.append(" ")
                index += 1
            continue
        if character == "/" and following == "*":
            output.extend("  ")
            index += 2
            while index < len(value):
                if index + 1 < len(value) and value[index : index + 2] == "*/":
                    output.extend("  ")
                    index += 2
                    break
                output.append(value[index] if value[index] in "\r\n" else " ")
                index += 1
            continue
        output.append(character)
        index += 1
    return "".join(output)


def repository_root() -> Path:
    candidates = (Path.cwd(), Path(__file__).resolve().parents[2])
    for candidate in candidates:
        if (
            (candidate / "pyproject.toml").is_file()
            and (candidate / "extensions" / "github-workflows").is_dir()
            and (candidate / "codex" / "skills").is_dir()
        ):
            return candidate.resolve()
    raise RuntimeError("run agent-workflows install from an agent-workflows checkout")


def command_path(name: str) -> str | None:
    candidate = shutil.which(name)
    if candidate:
        return candidate
    local = Path.home() / ".local" / "bin" / name
    return str(local) if local.is_file() and os.access(local, os.X_OK) else None


def resolved_link(path: Path) -> Path | None:
    if not path.is_symlink():
        return None
    try:
        return path.resolve(strict=True)
    except OSError:
        return None


def extension_source(target: Path) -> Path | None:
    metadata = target / ".qwen-extension-install.json"
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("type") != "link" or not isinstance(payload.get("source"), str):
        return None
    try:
        return Path(payload["source"]).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


class Installer:
    def __init__(self, args: argparse.Namespace, root: Path | None = None) -> None:
        self.args = args
        self.root = (root or repository_root()).resolve()
        self.home = Path.home()
        self.cache = Path(os.environ.get("XDG_CACHE_HOME", self.home / ".cache"))
        self.changes: list[str] = []
        self.warnings: list[str] = []
        self.changed_components: set[str] = set()
        self.mcp_changes: set[tuple[str, str]] = set()
        self.change_groups: dict[str, str] = {}
        self.change_components: dict[str, str] = {}

    def add_change(self, description: str, *, group: str, component: str) -> None:
        """Register one independently selectable installation change."""
        self.changes.append(description)
        self.change_groups[description] = group
        self.change_components[description] = component
        self.changed_components.add(component)

    def notice(self, message: str) -> None:
        if self.args.verbose:
            print(f"[OK] {message}")

    def apply_notice(self, description: str) -> None:
        """Announce one selected mutation before it begins."""
        print(f"[APPLY] {self.change_groups.get(description, 'Shared')}: {description}")

    def run(self, *command: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            check=True,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def plan_runtime(self) -> None:
        uv = command_path("uv")
        if uv is None:
            raise RuntimeError("uv is required to install agent-workflows")
        state = self.root / ".venv" / ".agent-workflows-install.json"
        expected = {
            "dev": self.args.dev,
            "pyproject_sha256": hashlib.sha256(
                (self.root / "pyproject.toml").read_bytes(),
            ).hexdigest(),
        }
        try:
            current = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        executable = self.root / ".venv" / "bin" / "agent-workflows"
        if current != expected or not executable.is_file():
            self.add_change("install the Python environment", group="Shared", component="runtime")
        else:
            self.notice("Python environment is current")

    def plan_codex(self) -> None:
        destination = self.home / ".codex" / "skills"
        for source in sorted((self.root / "codex" / "skills").iterdir()):
            if not source.is_dir():
                continue
            target = destination / source.name
            current = resolved_link(target)
            if current == source.resolve():
                self.notice(f"Codex skill {source.name} is current")
            elif not target.exists() and not target.is_symlink():
                self.add_change(f"link skill {source.name}", group="Codex", component="codex")
            else:
                self.warnings.append(f"refusing unmanaged Codex skill: {target}")

    def user_policy_targets(self) -> tuple[tuple[Path, Path, str], ...]:
        return (
            (
                self.root / "user-policies" / "codex.md",
                self.home / ".codex" / "AGENTS.md",
                "Codex user instructions",
            ),
            (
                self.root / "user-policies" / "qwen.md",
                self.home / ".qwen" / "QWEN.md",
                "Qwen user instructions",
            ),
        )

    def rendered_user_policy(self, source: Path) -> str:
        sections = [USER_POLICY_MARKER, source.read_text(encoding="utf-8").strip()]
        if self.args.machine_role == "remote":
            sections.append(
                (self.root / "user-policies" / "remote-compute.md")
                .read_text(encoding="utf-8")
                .strip()
            )
        return "\n\n".join(sections) + "\n"

    def managed_user_policy(self, target: Path) -> bool:
        linked = resolved_link(target)
        sources = {source.resolve() for source, _target, _label in self.user_policy_targets()}
        if linked in sources:
            return True
        if not target.is_file() or target.is_symlink():
            return False
        content = target.read_text(encoding="utf-8")
        if content.startswith(USER_POLICY_MARKER):
            return True
        return any(content == source.read_text(encoding="utf-8") for source in sources)

    def plan_user_policies(self) -> None:
        for source, target, label in self.user_policy_targets():
            expected = self.rendered_user_policy(source)
            if target.is_file() and not target.is_symlink() and target.read_text() == expected:
                self.notice(f"{label} are current")
            elif (not target.exists() and not target.is_symlink()) or self.managed_user_policy(
                target
            ):
                group = "Codex" if "Codex" in label else "Qwen"
                self.add_change(
                    f"install {label}",
                    group=group,
                    component="user-policies",
                )
            else:
                self.warnings.append(f"refusing unmanaged {label}: {target}")

    def plan_qwen(self) -> None:
        if command_path("qwen") is None:
            self.warnings.append("Qwen Code is unavailable; extension linking will be skipped")
            return
        target = self.home / ".qwen" / "extensions" / "github-workflows"
        current = extension_source(target)
        source = self.root / "extensions" / "github-workflows"
        if current == source:
            self.notice("Qwen extension is current")
        elif not target.exists() and not target.is_symlink():
            self.add_change("link the github-workflows extension", group="Qwen", component="qwen")
        else:
            self.warnings.append(f"refusing unmanaged Qwen extension: {target}")

    def _inspect_mcp(self, client: str, executable: str) -> set[str] | None:
        """Return configured server names without connecting to them."""
        if client == "codex":
            configured: set[str] = set()
            for name in MCP_SERVERS:
                result = subprocess.run(
                    [executable, "mcp", "get", name, "--json"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    configured.add(name)
                elif "No MCP server named" not in result.stderr:
                    self.warnings.append(
                        f"could not inspect the Codex {name} MCP; installation will be skipped"
                    )
                    return None
            return configured

        configured_home = os.environ.get("QWEN_HOME")
        if configured_home:
            if configured_home == "~":
                qwen_home = self.home
            elif configured_home.startswith(("~/", "~\\")):
                qwen_home = self.home.joinpath(*configured_home[2:].replace("\\", "/").split("/"))
            else:
                qwen_home = Path(configured_home)
            if not qwen_home.is_absolute():
                qwen_home = Path.cwd() / qwen_home
            qwen_home = qwen_home.resolve()
        else:
            qwen_home = self.home / ".qwen"
        settings_path = qwen_home / "settings.json"
        try:
            payload = json.loads(strip_json_comments(settings_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return set()
        except (OSError, UnicodeError, json.JSONDecodeError):
            self.warnings.append(
                "could not inspect Qwen MCP settings; installation will be skipped"
            )
            return None
        servers = payload.get("mcpServers") if isinstance(payload, dict) else None
        if servers is None:
            return set()
        if not isinstance(servers, dict):
            self.warnings.append("Qwen MCP settings are invalid; installation will be skipped")
            return None
        return {name for name in MCP_SERVERS if name in servers}

    def plan_mcp(self) -> None:
        """Plan optional remote documentation and GitHub servers for both clients."""
        if not getattr(self.args, "install_mcp", True):
            return
        for client in ("codex", "qwen"):
            executable = command_path(client)
            label = client.capitalize()
            if executable is None:
                self.warnings.append(f"{label} is unavailable; MCP installation will be skipped")
                continue
            try:
                configured = self._inspect_mcp(client, executable)
            except OSError:
                configured = None
                self.warnings.append(
                    f"could not inspect {label} MCP servers; installation will be skipped"
                )
            if configured is None:
                continue
            for name in MCP_SERVERS:
                if name in configured:
                    self.notice(f"{label} {name} MCP is configured")
                    continue
                self.add_change(
                    f"configure the {name} MCP for {label}",
                    group=label,
                    component="mcp",
                )
                self.mcp_changes.add((client, name))

    def plan_polars(self) -> None:
        checkout = self.cache / "agent-workflows" / "upstream" / "polars-skills"
        missing: list[str] = []
        for agent in ("codex", "qwen"):
            target = self.home / f".{agent}" / "skills" / "polars"
            source = checkout / "polars"
            current = resolved_link(target)
            if current == source.resolve():
                self.notice(f"{agent} Polars skill is current")
            elif not target.exists() and not target.is_symlink():
                missing.append(agent.capitalize())
            else:
                self.warnings.append(f"refusing unmanaged Polars skill: {target}")
        if missing:
            clients = " and ".join(missing)
            group = clients if len(missing) == 1 else "Shared"
            self.add_change(
                f"install the official Polars skill for {clients}",
                group=group,
                component="polars",
            )

    @staticmethod
    def parse_exclusions(value: str, count: int) -> set[int]:
        """Parse yay-style numbers and ranges to exclude from an install list."""
        normalized = value.strip()
        if not normalized or normalized.lower() in {"n", "none"}:
            return set()
        if normalized.lower() in {"a", "all"}:
            return set(range(1, count + 1))
        excluded: set[int] = set()
        for token in normalized.replace(",", " ").split():
            if "-" in token:
                bounds = token.split("-", 1)
                if not all(bound.isdigit() for bound in bounds):
                    raise ValueError("selection must contain numbers or ranges")
                start, end = map(int, bounds)
                if start < 1 or end < start or end > count:
                    raise ValueError("selection is outside the listed range")
                excluded.update(range(start, end + 1))
            elif token.isdigit() and 1 <= int(token) <= count:
                excluded.add(int(token))
            else:
                raise ValueError("selection is outside the listed range")
        return excluded

    def print_changes(self) -> None:
        """Print stable client sections with numbering shared across sections."""
        print("Changes to apply:")
        groups = ("Codex", "Qwen", "Shared")
        self.changes = [
            change
            for group in groups
            for change in self.changes
            if self.change_groups.get(change, "Shared") == group
        ]
        numbered = {change: index for index, change in enumerate(self.changes, 1)}
        for group in groups:
            grouped = [
                change
                for change in self.changes
                if self.change_groups.get(change, "Shared") == group
            ]
            if not grouped:
                continue
            print(f"  {group}:")
            for change in grouped:
                print(f"    {numbered[change]}) {change}")

    def approve(self) -> bool:
        for warning in self.warnings:
            print(f"[WARN] {warning}", file=sys.stderr)
        if not self.changes:
            print("[OK] Nothing to do")
            return False
        self.print_changes()
        if self.args.dry_run:
            return False
        if self.args.yes:
            print("Installing all listed (--yes)")
            return True
        if not sys.stdin.isatty():
            raise RuntimeError("confirmation requires a terminal; rerun with --yes")
        while True:
            try:
                response = input(
                    "Exclude by number/range (for example 2 4-6; "
                    "Enter installs all; A excludes all): "
                )
            except EOFError:
                response = "A"
            try:
                excluded = self.parse_exclusions(response, len(self.changes))
            except ValueError:
                print(f"[WARN] Invalid selection; use numbers from 1 to {len(self.changes)}")
                continue
            break
        self.changes = [
            change for index, change in enumerate(self.changes, 1) if index not in excluded
        ]
        self.changed_components = {
            self.change_components[change]
            for change in self.changes
            if change in self.change_components
        }
        if not self.changes:
            print("[OK] Nothing selected")
            return False
        return True

    def apply_runtime(self) -> None:
        description = "install the Python environment"
        if description not in self.changes:
            return
        self.apply_notice(description)
        uv = command_path("uv")
        if uv is None:
            raise RuntimeError("uv disappeared during installation")
        python = self.root / ".venv" / "bin" / "python"
        if not python.is_file():
            self.run(uv, "venv", "--python", ">=3.12", str(self.root / ".venv"))
        requirement = f"{self.root}[dev]" if self.args.dev else str(self.root)
        self.run(uv, "pip", "install", "--python", str(python), "-e", requirement)
        state = {
            "dev": self.args.dev,
            "pyproject_sha256": hashlib.sha256(
                (self.root / "pyproject.toml").read_bytes(),
            ).hexdigest(),
        }
        state_path = self.root / ".venv" / ".agent-workflows-install.json"
        state_path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        if self.args.dev:
            pre_commit = self.root / ".venv" / "bin" / "pre-commit"
            self.run(str(pre_commit), "install")

    def replace_link(self, source: Path, target: Path) -> None:
        if target.is_symlink() or target.is_file():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source, target_is_directory=source.is_dir())

    def apply_user_policies(self) -> None:
        for source, target, label in self.user_policy_targets():
            if f"install {label}" not in self.changes:
                continue
            expected = self.rendered_user_policy(source)
            if target.is_file() and not target.is_symlink() and target.read_text() == expected:
                continue
            if (not target.exists() and not target.is_symlink()) or self.managed_user_policy(
                target
            ):
                self.apply_notice(f"install {label}")
                if target.is_symlink() or target.is_file():
                    target.unlink()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(expected, encoding="utf-8")

    def apply_codex(self) -> None:
        destination = self.home / ".codex" / "skills"
        for source in sorted((self.root / "codex" / "skills").iterdir()):
            if f"link skill {source.name}" not in self.changes:
                continue
            target = destination / source.name
            if resolved_link(target) == source.resolve():
                continue
            if not target.exists() and not target.is_symlink():
                self.apply_notice(f"link skill {source.name}")
                self.replace_link(source, target)

    def apply_qwen(self) -> None:
        if "link the github-workflows extension" not in self.changes:
            return
        qwen = command_path("qwen")
        if qwen is None:
            return
        target = self.home / ".qwen" / "extensions" / "github-workflows"
        current = extension_source(target)
        source = self.root / "extensions" / "github-workflows"
        if current == source:
            return
        if target.exists() or target.is_symlink():
            return
        self.apply_notice("link the github-workflows extension")
        self.run(qwen, "extensions", "link", str(source), input_text="y\n")

    def apply_mcp(self) -> None:
        """Register selected MCP servers through each client's native CLI."""
        for client, name in sorted(self.mcp_changes):
            label = client.capitalize()
            if f"configure the {name} MCP for {label}" not in self.changes:
                continue
            executable = command_path(client)
            if executable is None:
                raise RuntimeError(f"{client.capitalize()} disappeared during MCP installation")
            url = MCP_SERVERS[name]
            self.apply_notice(f"configure the {name} MCP for {label}")
            if client == "codex":
                self.run(executable, "mcp", "add", name, "--url", url)
            else:
                self.run(
                    executable,
                    "mcp",
                    "add",
                    "--scope",
                    "user",
                    "--transport",
                    "http",
                    name,
                    url,
                )

    def apply_polars(self) -> None:
        descriptions = [
            change for change in self.changes if self.change_components.get(change) == "polars"
        ]
        if not descriptions:
            return
        self.apply_notice(descriptions[0])
        checkout = self.cache / "agent-workflows" / "upstream" / "polars-skills"
        if not checkout.is_dir():
            checkout.parent.mkdir(parents=True, exist_ok=True)
            self.run("git", "clone", POLARS_URL, str(checkout))
        else:
            self.run("git", "-C", str(checkout), "fetch", "--quiet", "origin")
            branch = self.run("git", "-C", str(checkout), "branch", "--show-current").stdout.strip()
            if branch:
                local = self.run("git", "-C", str(checkout), "rev-parse", "HEAD").stdout.strip()
                remote = self.run(
                    "git",
                    "-C",
                    str(checkout),
                    "rev-parse",
                    f"origin/{branch}",
                ).stdout.strip()
                if local != remote:
                    self.run("git", "-C", str(checkout), "merge", "--ff-only", remote)
        source = checkout / "polars"
        if not source.is_dir():
            raise RuntimeError("the Polars checkout does not contain the polars skill")
        for agent in ("codex", "qwen"):
            target = self.home / f".{agent}" / "skills" / "polars"
            if resolved_link(target) != source.resolve() and (
                not target.exists() and not target.is_symlink()
            ):
                self.replace_link(source, target)

    def install(self) -> int:
        self.plan_runtime()
        self.plan_user_policies()
        self.plan_codex()
        self.plan_qwen()
        self.plan_mcp()
        self.plan_polars()
        if not self.approve():
            return 0
        if "runtime" in self.changed_components:
            self.apply_runtime()
        if "user-policies" in self.changed_components:
            self.apply_user_policies()
        if "codex" in self.changed_components:
            self.apply_codex()
        if "qwen" in self.changed_components:
            self.apply_qwen()
        if "mcp" in self.changed_components:
            self.apply_mcp()
        if "polars" in self.changed_components:
            self.apply_polars()
        print(f"[OK] Installation complete ({len(self.changes)} changes)")
        return 0


def add_install_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--machine-role",
        choices=("local", "remote"),
        default="local",
        help="include remote compute-environment instructions only on remote machines",
    )
    parser.add_argument(
        "--dev", action="store_true", help="also install development tools and Git hook"
    )
    parser.add_argument("--dry-run", action="store_true", help="show changes without applying them")
    parser.add_argument("--yes", "-y", action="store_true", help="approve all listed changes")
    parser.add_argument(
        "--skip-mcp",
        action="store_false",
        dest="install_mcp",
        help="do not offer GitHub and Context7 MCP installation for Codex or Qwen",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="also show unchanged integrations"
    )


def install_from_args(
    args: argparse.Namespace, factory: Callable[..., Installer] = Installer
) -> int:
    return factory(args).install()
