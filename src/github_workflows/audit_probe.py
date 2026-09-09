#!/usr/bin/env python3
"""Run a bounded repository-audit probe in an isolated child process."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path


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

PROBE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
WALL_SECONDS = 60
CPU_SECONDS = 45
GIT_SECONDS = 10
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
    try:
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
            timeout=GIT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"git {' '.join(args)} timed out after {GIT_SECONDS} seconds") from error
    return result.stdout.strip()


def environment_fingerprint(
    worktree: Path, python: Path, python_source: str, pythonpath: Path | None
) -> dict[str, object]:
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
        "pythonpath": str(pythonpath) if pythonpath is not None else None,
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
    return project_root, worktree, run_dir, python, python_source


def module_available(python: Path, module: str) -> bool:
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            f"import importlib.util; raise SystemExit(importlib.util.find_spec({module!r}) is None)",
        ],
        check=False,
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


def probe_readonly_roots(
    worktree: Path, venv: Path, python: Path, run_dir: Path
) -> list[Path | None]:
    """Read-only bind roots beyond the worktree itself.

    The venv when it lives outside the worktree; the venv's resolved target
    when it escapes the worktree through a symlink (a bind of the symlink
    path alone still leaves the target reachable by its real path); the
    interpreter's base CPython prefix; the primary checkout's git directory
    for a linked worktree; and the audit run state directory when it lives
    outside the worktree.
    """
    venv_target = venv.resolve()
    return [
        venv if not contained(venv, worktree) else None,
        venv_target if not contained(venv_target, worktree) else None,
        audit_sandbox.interpreter_prefix(python),
        audit_sandbox.primary_git_dir(worktree),
        run_dir if not contained(run_dir, worktree) else None,
    ]


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
            secure_directory(artifact_dir)
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
                "environment": environment_fingerprint(worktree, python, python_source, pythonpath),
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
    started = time.monotonic()
    address_space, nproc = audit_sandbox.current_resource_bounds()
    with tempfile.TemporaryDirectory(prefix="qwen-audit-probe-") as temporary:
        temporary_path = Path(temporary)
        environment = sanitized_environment(temporary_path, pythonpath)
        stdout_path = Path(temporary) / "stdout.txt"
        stderr_path = Path(temporary) / "stderr.txt"
        command = audit_sandbox.namespace_command(
            inner_command,
            worktree=worktree,
            readonly_binds=audit_sandbox.readonly_binds(
                worktree,
                probe_readonly_roots(worktree, python.parent.parent, python, run_dir),
                scratch=temporary_path,
            ),
            label="audit-probe",
        )
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                command,
                stdout=stdout,
                stderr=stderr,
                env=environment,
                start_new_session=True,
                preexec_fn=audit_sandbox.make_limits(
                    CPU_SECONDS, OUTPUT_BYTES, 256, address_space, nproc
                ),
            )
            returncode, timed_out = audit_sandbox.wait_bounded(process, WALL_SECONDS)
        after = git_output(worktree, "status", "--porcelain=v1", "--untracked-files=all")
        duration = time.monotonic() - started
        stdout_excerpt, stdout_truncated = audit_sandbox.read_bounded(
            stdout_path, EXCERPT_BYTES
        )
        stderr_excerpt, stderr_truncated = audit_sandbox.read_bounded(
            stderr_path, EXCERPT_BYTES
        )
        if before != after:
            probe_status = "worktree-modified"
        elif timed_out:
            probe_status = "timed-out"
        elif returncode == 0:
            probe_status = "succeeded"
        else:
            probe_status = "failed"
        # Create the artifact directory only once a result is recorded, so a
        # failed or refused attempt cannot poison the probe id.
        secure_directory(artifact_dir)
        stdout_path = shutil.move(stdout_path, artifact_dir / "stdout.txt")
        stderr_path = shutil.move(stderr_path, artifact_dir / "stderr.txt")
        result = {
            "schema_version": 1,
            "probe_id": args.probe_id,
            "probe": probe_identity,
            "repo_sha": git_output(worktree, "rev-parse", "HEAD"),
            "environment": environment_fingerprint(worktree, python, python_source, pythonpath),
            "probe_status": probe_status,
            "limits": {
                "wall_seconds": WALL_SECONDS,
                "cpu_seconds": CPU_SECONDS,
                "output_bytes_per_stream": OUTPUT_BYTES,
                "address_space_bytes": address_space,
                "nproc": nproc,
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
        result_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(stdout_path, 0o600)
        os.chmod(stderr_path, 0o600)
        os.chmod(result_path, 0o600)
    print(
        json.dumps(
            {
                "result": str(result_path),
                "probe_status": probe_status,
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
