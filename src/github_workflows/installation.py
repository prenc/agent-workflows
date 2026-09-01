"""Install the repository's Qwen extension and Codex skills."""

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

    def notice(self, message: str) -> None:
        if self.args.verbose:
            print(f"[OK] {message}")

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
            self.changes.append("install the Python environment")
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
                self.changes.append(f"link Codex skill {source.name}")
            else:
                self.warnings.append(f"refusing unmanaged Codex skill: {target}")

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
            self.changes.append("link the Qwen github-workflows extension")
        else:
            self.warnings.append(f"refusing unmanaged Qwen extension: {target}")

    def plan_polars(self) -> None:
        checkout = self.cache / "agent-workflows" / "upstream" / "polars-skills"
        if not checkout.is_dir():
            self.changes.append("clone the official Polars skill")
        for agent in ("codex", "qwen"):
            target = self.home / f".{agent}" / "skills" / "polars"
            source = checkout / "polars"
            current = resolved_link(target)
            if current == source.resolve():
                self.notice(f"{agent} Polars skill is current")
            elif not target.exists() and not target.is_symlink():
                self.changes.append(f"link the Polars skill for {agent}")
            else:
                self.warnings.append(f"refusing unmanaged Polars skill: {target}")

    def approve(self) -> bool:
        for warning in self.warnings:
            print(f"[WARN] {warning}", file=sys.stderr)
        if not self.changes:
            print("[OK] Nothing to do")
            return False
        print("Changes to apply:")
        for change in self.changes:
            print(f"  - {change}")
        if self.args.dry_run:
            return False
        if self.args.yes:
            return True
        if not sys.stdin.isatty():
            raise RuntimeError("confirmation requires a terminal; rerun with --yes")
        return input("Install all listed changes [Y/n] ").strip().lower() in {"", "y", "yes"}

    def apply_runtime(self) -> None:
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
        if target.is_symlink():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source, target_is_directory=True)

    def apply_codex(self) -> None:
        destination = self.home / ".codex" / "skills"
        for source in sorted((self.root / "codex" / "skills").iterdir()):
            target = destination / source.name
            if resolved_link(target) == source.resolve():
                continue
            if not target.exists() and not target.is_symlink():
                self.replace_link(source, target)

    def apply_qwen(self) -> None:
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
        self.run(qwen, "extensions", "link", str(source), input_text="y\n")

    def apply_polars(self) -> None:
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
        self.plan_codex()
        self.plan_qwen()
        self.plan_polars()
        if not self.approve():
            return 0
        self.apply_runtime()
        self.apply_codex()
        self.apply_qwen()
        self.apply_polars()
        print(f"[OK] Installation complete ({len(self.changes)} changes)")
        return 0


def add_install_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dev", action="store_true", help="also install development tools and Git hook"
    )
    parser.add_argument("--dry-run", action="store_true", help="show changes without applying them")
    parser.add_argument("--yes", "-y", action="store_true", help="approve all listed changes")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="also show unchanged integrations"
    )


def install_from_args(
    args: argparse.Namespace, factory: Callable[..., Installer] = Installer
) -> int:
    return factory(args).install()
