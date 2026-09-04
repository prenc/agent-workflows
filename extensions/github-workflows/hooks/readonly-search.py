#!/usr/bin/env python3
"""Bounded read-only ripgrep fallback for immutable audit worktrees."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
from pathlib import Path, PurePosixPath
from typing import Any

MAX_LIMIT = 200
MAX_OFFSET = 10_000
MAX_PATTERN = 4096
MAX_GLOBS = 20
MAX_TEXT = 500
TIMEOUT_SECONDS = 10
PRIVATE_NAMES = {".env", ".envrc", "id_ed25519", "id_rsa"}
PRIVATE_SUFFIXES = {".key", ".pem"}
EXCLUDED_GLOBS = (
    "!**/data/**",
    "!**/.env",
    "!**/.env.*",
    "!**/.envrc",
    "!**/secrets.*",
    "!**/*.key",
    "!**/*.pem",
    "!**/id_rsa",
    "!**/id_ed25519",
    "!**/.gitignore",
    "!**/.git",
    "!**/.git/**",
    "!**/.qwenignore",
    "!**/.agentignore",
    "!**/.aiignore",
)


class SearchError(ValueError):
    """A safe, user-facing search request failure."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise SearchError(message)


def parser() -> argparse.ArgumentParser:
    result = SafeArgumentParser(description=__doc__)
    subcommands = result.add_subparsers(dest="operation", required=True)
    for name in ("files", "search"):
        command = subcommands.add_parser(name)
        command.add_argument("--root", required=True)
        command.add_argument("--path", action="append", default=[])
        command.add_argument("--glob", action="append", default=[])
        command.add_argument("--limit", type=int, default=100)
        command.add_argument("--offset", type=int, default=0)
        if name == "search":
            command.add_argument("--pattern", required=True)
            command.add_argument("--fixed", action="store_true")
            command.add_argument("--ignore-case", action="store_true")
    return result


def private_path(path: PurePosixPath) -> bool:
    lowered = [part.lower() for part in path.parts]
    name = lowered[-1] if lowered else ""
    return (
        "data" in lowered
        or name in PRIVATE_NAMES
        or name.startswith((".env.", "secrets."))
        or PurePosixPath(name).suffix in PRIVATE_SUFFIXES
    )


def validate(args: argparse.Namespace) -> tuple[Path, list[str]]:
    root = Path(args.root)
    if not root.is_absolute() or not root.is_dir():
        raise SearchError("--root must be an existing absolute directory")
    root = root.resolve()
    git_file = root / ".git"
    if not root.name.startswith("gh-audit-repo-") or not git_file.is_file():
        raise SearchError("--root must be a managed immutable audit worktree")
    try:
        marker, raw_git_dir = git_file.read_text(encoding="utf-8").strip().split(":", 1)
        git_dir = Path(raw_git_dir.strip()).resolve()
    except (OSError, ValueError):
        raise SearchError("--root must be a managed immutable audit worktree") from None
    if marker != "gitdir" or "worktrees" not in git_dir.parts or not git_dir.is_dir():
        raise SearchError("--root must be a managed immutable audit worktree")
    if private_path(PurePosixPath(root.as_posix())):
        raise SearchError("the requested root is outside the readable audit surface")
    project_dir = os.environ.get("QWEN_CODE_PROJECT_DIR")
    if project_dir:
        try:
            root.relative_to(Path(project_dir).resolve())
        except ValueError:
            pass
        else:
            raise SearchError("private workflow storage cannot be searched")
    if not 1 <= args.limit <= MAX_LIMIT or not 0 <= args.offset <= MAX_OFFSET:
        raise SearchError(f"--limit must be 1..{MAX_LIMIT} and --offset must be 0..{MAX_OFFSET}")
    if len(args.glob) > MAX_GLOBS or any(len(value) > 256 for value in args.glob):
        raise SearchError("too many or overlong --glob values")
    if args.operation == "search" and len(args.pattern) > MAX_PATTERN:
        raise SearchError("--pattern is too long")

    paths = args.path or ["."]
    validated: list[str] = []
    for value in paths:
        candidate = PurePosixPath(value)
        if candidate.is_absolute() or ".." in candidate.parts or private_path(candidate):
            raise SearchError("--path must be a non-private relative path within --root")
        resolved = (root / value).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise SearchError("--path escapes --root") from exc
        validated.append(value)
    return root, validated


def build_command(args: argparse.Namespace, root: Path, paths: list[str]) -> list[str]:
    result = ["rg", "--hidden", "--no-ignore-parent", "--sort", "path"]
    for ignore_name in (".qwenignore", ".agentignore", ".aiignore"):
        ignore_file = root / ignore_name
        if ignore_file.is_file():
            result.extend(("--ignore-file", str(ignore_file)))
    for pattern in (*args.glob, *EXCLUDED_GLOBS):
        result.extend(("--glob", pattern))
    if args.operation == "files":
        result.append("--files")
    else:
        result.append("--json")
        if args.fixed:
            result.append("--fixed-strings")
        if args.ignore_case:
            result.append("--ignore-case")
        result.extend(("--", args.pattern))
    result.extend(paths)
    return result


def normalized_path(value: str) -> str:
    return value.removeprefix("./")


def run(args: argparse.Namespace, root: Path, paths: list[str]) -> tuple[list[Any], bool]:
    try:
        process = subprocess.Popen(
            build_command(args, root, paths),
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SearchError("ripgrep is unavailable") from exc
    timed_out = False

    def expire() -> None:
        nonlocal timed_out
        timed_out = True
        process.kill()

    timer = threading.Timer(TIMEOUT_SECONDS, expire)
    timer.start()
    items: list[Any] = []
    wanted = args.offset + args.limit + 1
    stopped_early = False
    assert process.stdout is not None
    try:
        for raw_line in process.stdout:
            if args.operation == "files":
                items.append(normalized_path(raw_line.decode("utf-8", "replace").rstrip("\n")))
            else:
                try:
                    event = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise SearchError("ripgrep returned an unreadable result") from exc
                if event.get("type") != "match":
                    continue
                data = event["data"]
                path = data["path"].get("text")
                match_text = data["lines"].get("text")
                if isinstance(path, str) and isinstance(match_text, str):
                    items.append(
                        {
                            "path": normalized_path(path),
                            "line": data["line_number"],
                            "text": match_text.rstrip()[:MAX_TEXT],
                        }
                    )
            if len(items) >= wanted:
                stopped_early = True
                process.terminate()
                break
        returncode = process.wait()
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
    if timed_out:
        raise SearchError("search exceeded the 10-second bound")
    if not stopped_early and returncode not in (0, 1):
        raise SearchError("ripgrep rejected the search request")
    return items, stopped_early


def main() -> int:
    try:
        args = parser().parse_args()
        root, paths = validate(args)
        all_items, stopped_early = run(args, root, paths)
        stop = args.offset + args.limit
        items = all_items[args.offset : stop]
        truncated = stopped_early or stop < len(all_items)
        print(
            json.dumps(
                {
                    "operation": args.operation,
                    "items": items,
                    "offset": args.offset,
                    "limit": args.limit,
                    "truncated": truncated,
                    "next_offset": stop if truncated else None,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except SearchError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
