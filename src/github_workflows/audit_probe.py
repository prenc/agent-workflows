#!/usr/bin/env python3
"""Run a bounded repository-audit probe in an isolated child process."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

PROBE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
WALL_SECONDS = 60
CPU_SECONDS = 45
OUTPUT_BYTES = 10 * 1024 * 1024
EXCERPT_BYTES = 256 * 1024
INLINE_CODE_BYTES = 8 * 1024


def contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_output(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(worktree),
            *args,
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def environment_fingerprint(worktree: Path, python: Path, python_source: str) -> dict[str, object]:
    package_script = """
import importlib.metadata as metadata
import json
import platform

packages = {}
for name in ("polars", "numpy", "pandas", "pytest"):
    try:
        packages[name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        packages[name] = None
print(json.dumps({"python": platform.python_version(), "packages": packages}))
"""
    # Avoid importing packages; metadata lookup is sufficient and lightweight.
    versions: dict[str, object] = {"python": "unknown", "packages": {}}
    try:
        result = subprocess.run(
            [str(python), "-c", package_script],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
        versions = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass

    lockfiles = {}
    for name in ("uv.lock", "pyproject.toml", "requirements.txt"):
        candidate = worktree / name
        if candidate.is_file():
            lockfiles[name] = sha256_file(candidate)
    return {
        "python_executable": str(python),
        "python_source": python_source,
        "versions": versions,
        "lockfiles": lockfiles,
    }


def validate_common(args: argparse.Namespace) -> tuple[Path, Path, Path, Path, str]:
    project_root = args.project_root.expanduser().resolve()
    worktree = args.audit_worktree.expanduser().resolve()
    run_dir = args.run_dir.expanduser().resolve()
    if not project_root.is_dir() or not worktree.is_dir():
        raise ValueError("project root and audit worktree must be existing directories")
    configured = args.project_dir or os.environ.get("QWEN_CODE_PROJECT_DIR")
    if not configured:
        raise ValueError(
            "QWEN_CODE_PROJECT_DIR is required outside tests; use --project-dir explicitly"
        )
    expected = Path(configured).expanduser().resolve() / "workflows" / "gh-audit-repo" / "current"
    if run_dir != expected or not run_dir.is_dir():
        raise ValueError("run directory must be the current project-local audit run")
    if not PROBE_ID_RE.fullmatch(args.probe_id):
        raise ValueError("probe id contains unsupported characters")
    candidate = project_root / ".venv" / "bin" / "python"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        # Keep the venv launch path: resolving the symlink yields the base
        # interpreter, which loses pyvenv.cfg detection and the venv site-packages.
        python = candidate
        python_source = "project-venv"
    else:
        python = Path(sys.executable).resolve()
        python_source = "system"
    artifact_dir = run_dir / "validation" / args.probe_id
    if artifact_dir.exists() and any(artifact_dir.iterdir()):
        raise ValueError("probe id already has artifacts; use a unique id for every attempt")
    secure_directory(artifact_dir)
    return project_root, worktree, run_dir, python, python_source


def module_available(python: Path, module: str) -> bool:
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            f"import importlib.util; raise SystemExit(importlib.util.find_spec({module!r}) is None)",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return result.returncode == 0


def validate_selector(value: str, worktree: Path) -> str:
    path_part = value.split("::", 1)[0]
    selector_path = Path(path_part)
    if selector_path.is_absolute() or ".." in selector_path.parts:
        raise ValueError(f"pytest selector must be relative to the audit worktree: {value}")
    resolved = (worktree / selector_path).resolve()
    if not contained(resolved, worktree) or not resolved.exists():
        raise ValueError(f"pytest selector does not resolve inside the audit worktree: {value}")
    return value


def sanitized_environment(temp_root: Path, pythonpath: Path | None) -> dict[str, str]:
    home = temp_root / "home"
    cache = temp_root / "cache"
    temp = temp_root / "tmp"
    for path in (home, cache, temp):
        secure_directory(path)
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "XDG_CACHE_HOME": str(cache),
        "TMPDIR": str(temp),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "POLARS_MAX_THREADS": "1",
        "RAYON_NUM_THREADS": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "WANDB_MODE": "disabled",
    }
    if pythonpath is not None:
        environment["PYTHONPATH"] = str(pythonpath)
    return environment


def namespace_command(worktree: Path, venv: Path, command: Sequence[str]) -> list[str]:
    script = (
        "set -eu\n"
        'worktree="$1"\n'
        'venv="$2"\n'
        "shift 2\n"
        '/usr/bin/mount --bind "$worktree" "$worktree"\n'
        '/usr/bin/mount -o remount,bind,ro "$worktree"\n'
        'if [ "${venv#"$worktree"/}" = "$venv" ]; then\n'
        '  /usr/bin/mount --bind "$venv" "$venv"\n'
        '  /usr/bin/mount -o remount,bind,ro "$venv"\n'
        "fi\n"
        'cd "$worktree"\n'
        'exec "$@"\n'
    )
    return [
        "/usr/bin/unshare",
        "--user",
        "--map-root-user",
        "--mount",
        "--net",
        "/bin/sh",
        "-c",
        script,
        "audit-probe",
        str(worktree),
        str(venv),
        *command,
    ]


def limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_BYTES, OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.nice(10)


def read_excerpt(path: Path) -> tuple[str, bool]:
    content = path.read_bytes()
    truncated = len(content) > EXCERPT_BYTES
    if truncated:
        content = content[:EXCERPT_BYTES]
    return content.decode("utf-8", errors="replace"), truncated


def run_probe(args: argparse.Namespace) -> int:
    _, worktree, run_dir, python, python_source = validate_common(args)
    artifact_dir = run_dir / "validation" / args.probe_id
    pythonpath = None
    if args.pythonpath:
        pythonpath = (worktree / args.pythonpath).resolve()
        if not contained(pythonpath, worktree) or not pythonpath.is_dir():
            raise ValueError("pythonpath must resolve to a directory in the audit worktree")

    if args.kind == "pytest":
        selectors = [validate_selector(value, worktree) for value in args.selector]
        if not selectors:
            raise ValueError("at least one focused pytest selector is required")
        if not module_available(python, "pytest"):
            artifact_dir = run_dir / "validation" / args.probe_id
            stdout_path = artifact_dir / "stdout.txt"
            stderr_path = artifact_dir / "stderr.txt"
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(
                "pytest is unavailable in the selected interpreter\n", encoding="utf-8"
            )
            result = {
                "schema_version": 1,
                "probe_id": args.probe_id,
                "probe": {"kind": "pytest", "selectors": selectors},
                "probe_status": "unavailable",
                "reason": "pytest is unavailable in the selected interpreter",
                "repo_sha": git_output(worktree, "rev-parse", "HEAD"),
                "environment": environment_fingerprint(worktree, python, python_source),
                "returncode": None,
                "timed_out": False,
                "worktree_unchanged": True,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            }
            result_path = artifact_dir / "result.json"
            result_path.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            for path in (stdout_path, stderr_path, result_path):
                os.chmod(path, 0o600)
            print(json.dumps({"result": str(result_path), "probe_status": "unavailable"}))
            return 0
        inner_command = [
            str(python),
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *selectors,
        ]
        probe_identity: dict[str, object] = {"kind": "pytest", "selectors": selectors}
    else:
        encoded_code = args.code.encode("utf-8")
        if not encoded_code or len(encoded_code) > INLINE_CODE_BYTES:
            raise ValueError(f"inline Python must contain 1-{INLINE_CODE_BYTES} UTF-8 bytes")
        if "\x00" in args.code:
            raise ValueError("inline Python must not contain NUL characters")
        inner_command = [str(python), "-c", args.code]
        probe_identity = {
            "kind": "python",
            "code": args.code,
            "code_sha256": hashlib.sha256(encoded_code).hexdigest(),
        }

    before = git_output(worktree, "status", "--porcelain=v1", "--untracked-files=all")
    stdout_path = artifact_dir / "stdout.txt"
    stderr_path = artifact_dir / "stderr.txt"
    started = time.monotonic()
    timed_out = False
    with tempfile.TemporaryDirectory(prefix="qwen-audit-probe-") as temporary:
        environment = sanitized_environment(Path(temporary), pythonpath)
        command = namespace_command(worktree, python.parent.parent, inner_command)
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                command,
                stdout=stdout,
                stderr=stderr,
                env=environment,
                start_new_session=True,
                preexec_fn=limits,
            )
            try:
                returncode = process.wait(timeout=WALL_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    returncode = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    returncode = process.wait()
    duration = time.monotonic() - started
    after = git_output(worktree, "status", "--porcelain=v1", "--untracked-files=all")
    stdout_excerpt, stdout_truncated = read_excerpt(stdout_path)
    stderr_excerpt, stderr_truncated = read_excerpt(stderr_path)
    result = {
        "schema_version": 1,
        "probe_id": args.probe_id,
        "probe": probe_identity,
        "repo_sha": git_output(worktree, "rev-parse", "HEAD"),
        "environment": environment_fingerprint(worktree, python, python_source),
        "probe_status": "timed-out"
        if timed_out
        else ("succeeded" if returncode == 0 else "failed"),
        "limits": {
            "wall_seconds": WALL_SECONDS,
            "cpu_seconds": CPU_SECONDS,
            "output_bytes_per_stream": OUTPUT_BYTES,
            "threads": 1,
            "network_namespace": True,
            "read_only_worktree_mount": True,
        },
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_seconds": round(duration, 6),
        "worktree_unchanged": before == after,
        "worktree_status_before": before,
        "worktree_status_after": after,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "stdout_excerpt": stdout_excerpt,
        "stderr_excerpt": stderr_excerpt,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }
    result_path = artifact_dir / "result.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(stdout_path, 0o600)
    os.chmod(stderr_path, 0o600)
    os.chmod(result_path, 0o600)
    print(
        json.dumps(
            {
                "result": str(result_path),
                "returncode": returncode,
                "timed_out": timed_out,
                "worktree_unchanged": before == after,
            }
        )
    )
    if before != after:
        return 3
    return 124 if timed_out else returncode


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project-root", required=True, type=Path)
    common.add_argument("--project-dir", type=Path)
    common.add_argument("--audit-worktree", required=True, type=Path)
    common.add_argument("--run-dir", required=True, type=Path)
    common.add_argument("--probe-id", required=True)
    common.add_argument("--pythonpath", type=Path)
    subparsers = result.add_subparsers(dest="kind", required=True)
    pytest_parser = subparsers.add_parser("pytest", parents=[common])
    pytest_parser.add_argument("--selector", action="append", default=[])
    python_parser = subparsers.add_parser("python", parents=[common])
    python_parser.add_argument(
        "--code",
        required=True,
        help="visible inline Python passed to the project interpreter with -c",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return run_probe(args)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"audit probe refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
