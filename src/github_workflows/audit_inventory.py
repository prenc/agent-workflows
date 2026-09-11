#!/usr/bin/env python3
"""Maintain a private, revisioned environment inventory for repository audits."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from typing import Any


def _load_audit_sandbox() -> types.ModuleType:
    """Load the sibling audit_sandbox module without importing the package.

    These helper CLIs run as standalone scripts. Importing the
    ``github_workflows`` package executes import-time side effects (including
    ``github_cache``'s interpreter bootstrap, which can replace the running
    process), so the helper loads its sibling module directly from its own
    directory.
    """
    module_file = Path(__file__).resolve().with_name("audit_sandbox.py")
    spec = importlib.util.spec_from_file_location("audit_sandbox", module_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"sibling audit_sandbox module is unavailable: {module_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_sandbox = _load_audit_sandbox()

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
ALLOWED_ARGUMENTS = {"--version", "-V", "-v", "version", "--help", "-h"}
PROGRAM_ARGUMENTS = {"tmux": ["-V"]}
WALL_SECONDS = 10
OUTPUT_BYTES = 256 * 1024


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def secure_file(path: Path) -> None:
    os.chmod(path, 0o600)


def validate_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    project = args.project_root.expanduser().resolve()
    worktree = args.audit_worktree.expanduser().resolve()
    run_dir = args.run_dir.expanduser().resolve()
    if not project.is_dir() or not worktree.is_dir() or not run_dir.is_dir():
        raise ValueError("project root, audit worktree, and run directory must exist")
    configured = args.project_dir or os.environ.get("QWEN_CODE_PROJECT_DIR")
    if not configured:
        raise ValueError(
            "QWEN_CODE_PROJECT_DIR is required outside tests; use --project-dir explicitly"
        )
    expected = Path(configured).expanduser().resolve() / "workflows" / "gh-audit-repo" / "current"
    if run_dir != expected:
        raise ValueError("run directory must be the current project-local audit run")
    return project, worktree, run_dir


def inventory_path(run_dir: Path) -> Path:
    return run_dir / "state.json"


def load_inventory(run_dir: Path) -> dict[str, Any]:
    path = inventory_path(run_dir)
    if not path.is_file():
        raise ValueError("current audit state is missing")
    state = json.loads(path.read_text(encoding="utf-8"))
    value = state.get("inventory") if isinstance(state, dict) else None
    if not isinstance(value, dict):
        raise ValueError("environment inventory has not been initialized")
    if value.get("schema_version") != 1 or not isinstance(value.get("revision"), int):
        raise ValueError("environment inventory schema is incompatible")
    return value


def write_inventory(run_dir: Path, value: dict[str, Any], event: dict[str, Any]) -> int:
    path = inventory_path(run_dir)
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("revision"), int):
        raise ValueError("current audit state is incompatible")
    state["inventory"] = value
    state["revision"] += 1
    state["updated_at"] = event["timestamp"]
    temporary = path.with_suffix(".json.new")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    secure_file(temporary)
    os.replace(temporary, path)
    secure_file(path)
    journal = run_dir / "journal.jsonl"
    if journal.is_file():
        journal_event = {
            **event,
            "event": event.get("type", "inventory_updated"),
            "inventory_revision": event.get("revision"),
            "state_revision": state["revision"],
        }
        journal_event.pop("type", None)
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(journal_event, sort_keys=True) + "\n")
        secure_file(journal)
    return state["revision"]


def selected_python(project: Path) -> tuple[Path, str]:
    candidate = project / ".venv" / "bin" / "python"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        # Preserve the venv launch path. Resolving its interpreter symlink can
        # bypass pyvenv.cfg and silently enumerate the base environment.
        return candidate, "project-venv"
    return Path(sys.executable).resolve(), "system"


def package_inventory(project: Path) -> dict[str, Any]:
    python, source = selected_python(project)
    code = (
        "import importlib.metadata as m,json,platform,sys,sysconfig;"
        "print(json.dumps({'python':platform.python_version(),"
        "'interpreter_prefix':sys.prefix,'stdlib_root':sysconfig.get_path('stdlib'),"
        "'packages':dict(sorted((d.metadata.get('Name',d.name),d.version) for d in m.distributions()))}))"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="qwen-audit-inventory-") as temporary:
            stdout_path = Path(temporary) / "stdout"
            stderr_path = Path(temporary) / "stderr"
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                process = subprocess.Popen(
                    [str(python), "-I", "-c", code],
                    stdout=stdout,
                    stderr=stderr,
                    env={
                        "PATH": "/usr/bin:/bin",
                        "LANG": "C.UTF-8",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    start_new_session=True,
                )
                returncode, _ = audit_sandbox.wait_bounded(process, WALL_SECONDS)
            if returncode != 0:
                raise subprocess.CalledProcessError(returncode, [str(python)])
            payload_text, _ = audit_sandbox.read_bounded(stdout_path, OUTPUT_BYTES)
        payload = json.loads(payload_text)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {
            "available": False,
            "source": source,
            "executable": "python",
            "reason": "Python package inventory failed",
            "packages": {},
        }
    return {"available": True, "source": source, "executable": "python", **payload}


def manifest_inventory(worktree: Path) -> dict[str, Any]:
    names = (
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "package.json",
        "package-lock.json",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
    )
    result: dict[str, Any] = {}
    for name in names:
        path = worktree / name
        if path.is_file():
            content = path.read_bytes()
            result[name] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    return result


def build_initial_inventory(project: Path, worktree: Path) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": 1,
        "revision": 1,
        "created_at": now,
        "updated_at": now,
        "sources": {
            "python_environment": package_inventory(project),
            "repository_manifests": manifest_inventory(worktree),
            "programs": {},
            "declared": {},
            "context": {},
        },
        "requests": {},
    }


def commit_initial_inventory(run_dir: Path, value: dict[str, Any]) -> dict[str, Any]:
    path = inventory_path(run_dir)
    state = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(state, dict) and "inventory" in state:
        raise ValueError("environment inventory already exists")
    state_revision = write_inventory(
        run_dir,
        value,
        {"type": "inventory_initialized", "revision": 1, "timestamp": value["updated_at"]},
    )
    return {"inventory": str(path), "revision": 1, "state_revision": state_revision}


def initialize(args: argparse.Namespace) -> None:
    project, worktree, run_dir = validate_paths(args)
    state = json.loads(inventory_path(run_dir).read_text(encoding="utf-8"))
    if isinstance(state, dict) and "inventory" in state:
        raise ValueError("environment inventory already exists")
    value = build_initial_inventory(project, worktree)
    print(json.dumps(commit_initial_inventory(run_dir, value)))


def refresh_sources(project: Path, worktree: Path) -> dict[str, Any]:
    return {
        "python_environment": package_inventory(project),
        "repository_manifests": manifest_inventory(worktree),
    }


def commit_refresh(
    run_dir: Path, current: dict[str, Any], expected_revision: int
) -> dict[str, Any]:
    value = load_inventory(run_dir)
    check_revision(value, expected_revision)
    previous = {
        "python_environment": value["sources"]["python_environment"],
        "repository_manifests": value["sources"]["repository_manifests"],
    }
    changed = previous != current
    value["sources"].update(current)
    now = utc_now()
    value["revision"] += 1
    value["updated_at"] = now
    state_revision = write_inventory(
        run_dir,
        value,
        {
            "type": "inventory_refreshed",
            "revision": value["revision"],
            "changed": changed,
            "timestamp": now,
        },
    )
    return {
        "inventory": str(inventory_path(run_dir)),
        "revision": value["revision"],
        "state_revision": state_revision,
        "changed": changed,
    }


def refresh(args: argparse.Namespace) -> None:
    project, worktree, run_dir = validate_paths(args)
    value = load_inventory(run_dir)
    check_revision(value, args.expected_revision)
    current = refresh_sources(project, worktree)
    print(json.dumps(commit_refresh(run_dir, current, args.expected_revision)))


def probe_readonly_roots(
    worktree: Path,
    run_dir: Path,
    executable: Path,
    readonly_root: Path | None,
) -> list[Path | None]:
    """Read-only bind roots beyond the worktree itself for program probes.

    The project .venv when it lives outside the worktree, its resolved
    target when the venv escapes the worktree through a symlink, the
    executable's base interpreter prefix, the primary checkout's git
    directory for a linked worktree, and the audit run state directory when
    it lives outside the worktree.
    """
    venv_target = readonly_root.resolve() if readonly_root is not None else None
    return [
        readonly_root
        if readonly_root is not None and not contained(readonly_root, worktree)
        else None,
        venv_target if venv_target is not None and not contained(venv_target, worktree) else None,
        audit_sandbox.interpreter_prefix(executable),
        audit_sandbox.primary_git_dir(worktree),
        run_dir if not contained(run_dir, worktree) else None,
    ]


def sanitized_environment(root: Path, executable: Path) -> dict[str, str]:
    values = {}
    for name in ("home", "cache", "config", "data", "runtime", "tmp"):
        path = root / name
        secure_directory(path)
        values[name] = str(path)
    return {
        "PATH": f"{executable.parent}:/usr/bin:/bin",
        "HOME": values["home"],
        "XDG_CACHE_HOME": values["cache"],
        "XDG_CONFIG_HOME": values["config"],
        "XDG_DATA_HOME": values["data"],
        "XDG_RUNTIME_DIR": values["runtime"],
        "TMPDIR": values["tmp"],
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def check_revision(value: dict[str, Any], expected: int) -> None:
    if value["revision"] != expected:
        raise RuntimeError(
            f"inventory revision conflict: expected {expected}, found {value['revision']}"
        )


def probe_program(
    project: Path, worktree: Path, run_dir: Path, name: str, arguments: list[str]
) -> dict[str, Any]:
    if not NAME_RE.fullmatch(name):
        raise ValueError("program name contains unsupported characters")
    arguments = arguments or PROGRAM_ARGUMENTS.get(name, ["--version"])
    if not arguments or any(argument not in ALLOWED_ARGUMENTS for argument in arguments):
        raise ValueError("program probes accept only version/help arguments")
    project_venv = project / ".venv"
    venv_candidate = project_venv / "bin" / name
    if venv_candidate.is_file() and os.access(venv_candidate, os.X_OK):
        executable_name = str(venv_candidate)
        executable_source = "project-venv"
        readonly_root: Path | None = project_venv
    else:
        executable_name = shutil.which(name)
        executable_source = "system-path"
        readonly_root = None
    now = utc_now()
    fact: dict[str, Any]
    if executable_name is None:
        fact = {
            "available": False,
            "probe_status": "not-found",
            "arguments": arguments,
            "reason": "executable not found",
            "collected_at": now,
            "source": "current-audit-host",
        }
    else:
        executable = Path(executable_name).absolute()
        resolved_executable = executable.resolve()
        forbidden = (worktree, run_dir)
        if any(contained(resolved_executable, root) for root in forbidden):
            raise ValueError("program executable must resolve outside project and run directories")
        if executable_source != "project-venv" and contained(resolved_executable, project):
            raise ValueError("project executables must be installed in the project .venv")
        with tempfile.TemporaryDirectory(prefix="qwen-audit-inventory-") as temporary:
            temporary_path = Path(temporary)
            environment = sanitized_environment(temporary_path, executable)
            stdout_path = temporary_path / "stdout"
            stderr_path = temporary_path / "stderr"
            address_space, nproc = audit_sandbox.current_resource_bounds()
            command = audit_sandbox.namespace_command(
                [str(executable), *arguments],
                worktree=worktree,
                scratch=temporary_path,
                scratch_bytes=audit_sandbox.SCRATCH_BYTES,
                readonly_binds=audit_sandbox.readonly_binds(
                    worktree,
                    probe_readonly_roots(worktree, run_dir, executable, readonly_root),
                    scratch=temporary_path,
                ),
                label="audit-inventory",
                working_directory=temporary_path,
            )
            try:
                with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                    process = subprocess.Popen(
                        command,
                        stdout=stdout,
                        stderr=stderr,
                        env=environment,
                        start_new_session=True,
                        preexec_fn=audit_sandbox.make_limits(
                            WALL_SECONDS, OUTPUT_BYTES, 128, address_space, nproc
                        ),
                    )
                    returncode, timed_out = audit_sandbox.wait_bounded(process, WALL_SECONDS)
            except OSError as error:
                fact = {
                    "available": True,
                    "probe_status": "failed",
                    "executable": name,
                    "executable_source": executable_source,
                    "arguments": arguments,
                    "reason": str(error),
                    "collected_at": now,
                    "source": "current-audit-host",
                }
            else:
                if timed_out:
                    fact = {
                        "available": True,
                        "probe_status": "timed-out",
                        "executable": name,
                        "executable_source": executable_source,
                        "arguments": arguments,
                        "reason": "probe timed out",
                        "collected_at": now,
                        "source": "current-audit-host",
                    }
                else:
                    stdout, stdout_truncated = audit_sandbox.read_bounded(stdout_path, OUTPUT_BYTES)
                    stderr, stderr_truncated = audit_sandbox.read_bounded(stderr_path, OUTPUT_BYTES)
                    fact = {
                        "available": True,
                        "probe_status": "succeeded" if returncode == 0 else "failed",
                        "executable": name,
                        "executable_source": executable_source,
                        "arguments": arguments,
                        "returncode": returncode,
                        "stdout": stdout,
                        "stderr": stderr,
                        "truncated": stdout_truncated or stderr_truncated,
                        "collected_at": now,
                        "source": "current-audit-host",
                    }
    return fact


def collect_program_facts(
    project: Path, worktree: Path, run_dir: Path, probes: list[Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    facts: dict[str, dict[str, Any]] = {}
    request_ids: dict[str, str] = {}
    for probe in probes:
        if not isinstance(probe, dict) or not isinstance(probe.get("name"), str):
            raise ValueError("each program probe requires a name")
        name = probe["name"]
        arguments = probe.get("arguments", [])
        request_id = probe.get("request_id")
        if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
            raise ValueError(f"program {name} arguments must be a list of strings")
        if request_id is not None:
            if not isinstance(request_id, str) or not NAME_RE.fullmatch(request_id):
                raise ValueError(f"program {name} request_id contains unsupported characters")
            request_ids[request_id] = name
        facts[name] = probe_program(project, worktree, run_dir, name, arguments)
    return facts, request_ids


def commit_program_facts(
    run_dir: Path,
    facts: dict[str, dict[str, Any]],
    request_ids: dict[str, str],
    expected_revision: int,
) -> dict[str, Any]:
    value = load_inventory(run_dir)
    check_revision(value, expected_revision)
    value["sources"]["programs"].update(facts)
    for request_id, name in request_ids.items():
        value["requests"][request_id] = {"status": "resolved", "fact": f"program:{name}"}
    value["revision"] += 1
    value["updated_at"] = utc_now()
    state_revision = write_inventory(
        run_dir,
        value,
        {
            "type": "inventory_updated",
            "revision": value["revision"],
            "fact": "programs",
            "facts": [f"program:{name}" for name in facts],
            "timestamp": value["updated_at"],
        },
    )
    return {
        "inventory": str(inventory_path(run_dir)),
        "revision": value["revision"],
        "state_revision": state_revision,
        "facts": facts,
    }


def inspect_programs(args: argparse.Namespace) -> None:
    project, worktree, run_dir = validate_paths(args)
    probes = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(probes, list) or not probes:
        raise ValueError("program probes input must be a non-empty JSON list")
    value = load_inventory(run_dir)
    check_revision(value, args.expected_revision)
    facts, request_ids = collect_program_facts(project, worktree, run_dir, probes)
    print(json.dumps(commit_program_facts(run_dir, facts, request_ids, args.expected_revision)))


def record_facts(args: argparse.Namespace) -> None:
    _, _, run_dir = validate_paths(args)
    value = load_inventory(run_dir)
    check_revision(value, args.expected_revision)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("declared facts input must be a JSON object")
    value["sources"]["declared"].update(payload)
    now = utc_now()
    value["revision"] += 1
    value["updated_at"] = now
    state_revision = write_inventory(
        run_dir,
        value,
        {
            "type": "inventory_updated",
            "revision": value["revision"],
            "fact": "declared",
            "timestamp": now,
        },
    )
    print(
        json.dumps(
            {
                "inventory": str(inventory_path(run_dir)),
                "revision": value["revision"],
                "state_revision": state_revision,
            }
        )
    )


def record_context(args: argparse.Namespace) -> None:
    _, _, run_dir = validate_paths(args)
    if not NAME_RE.fullmatch(args.request_id):
        raise ValueError("request-id contains unsupported characters")
    value = load_inventory(run_dir)
    check_revision(value, args.expected_revision)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("context fact input must be a JSON object")
    value["sources"]["context"][args.request_id] = payload
    value["requests"][args.request_id] = {
        "status": "resolved",
        "fact": f"context:{args.request_id}",
    }
    now = utc_now()
    value["revision"] += 1
    value["updated_at"] = now
    state_revision = write_inventory(
        run_dir,
        value,
        {
            "type": "inventory_updated",
            "revision": value["revision"],
            "fact": f"context:{args.request_id}",
            "timestamp": now,
        },
    )
    print(
        json.dumps(
            {
                "inventory": str(inventory_path(run_dir)),
                "revision": value["revision"],
                "state_revision": state_revision,
            }
        )
    )


def status(args: argparse.Namespace) -> None:
    _, _, run_dir = validate_paths(args)
    value = load_inventory(run_dir)
    print(json.dumps(value, indent=2, sort_keys=True))


def common(command: argparse.ArgumentParser) -> None:
    command.add_argument("--project-root", type=Path, required=True)
    command.add_argument("--project-dir", type=Path)
    command.add_argument("--audit-worktree", type=Path, required=True)
    command.add_argument("--run-dir", type=Path, required=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    command = sub.add_parser("initialize")
    common(command)
    command.set_defaults(handler=initialize)
    command = sub.add_parser("refresh")
    common(command)
    command.add_argument("--expected-revision", type=int, required=True)
    command.set_defaults(handler=refresh)
    command = sub.add_parser("program")
    common(command)
    command.add_argument("--input", type=Path, required=True)
    command.add_argument("--expected-revision", type=int, required=True)
    command.set_defaults(handler=inspect_programs)
    command = sub.add_parser("record-declared")
    common(command)
    command.add_argument("--input", type=Path, required=True)
    command.add_argument("--expected-revision", type=int, required=True)
    command.set_defaults(handler=record_facts)
    command = sub.add_parser("record-context")
    common(command)
    command.add_argument("--request-id", required=True)
    command.add_argument("--input", type=Path, required=True)
    command.add_argument("--expected-revision", type=int, required=True)
    command.set_defaults(handler=record_context)
    command = sub.add_parser("status")
    common(command)
    command.set_defaults(handler=status)
    return root


def main() -> int:
    args = parser().parse_args()
    args.handler(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as error:
        print(f"audit-inventory: {error}", file=os.sys.stderr)
        raise SystemExit(2)
