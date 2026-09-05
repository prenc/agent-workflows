#!/usr/bin/env python3
"""Bounded read-only ripgrep fallback for immutable audit worktrees."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Any

MAX_LIMIT = 200
MAX_OFFSET = 10_000
MAX_PATTERN = 4096
MAX_GLOBS = 20
MAX_IGNORE_FILES = 10_000
MAX_TRACKED_SYMLINKS = 10_000
MAX_TEXT = 500
TIMEOUT_SECONDS = 10
PRIVATE_NAMES = {
    ".agentignore",
    ".aiignore",
    ".env",
    ".envrc",
    ".gitignore",
    ".ignore",
    ".qwenignore",
    "id_ed25519",
    "id_rsa",
}
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
    "!**/.ignore",
    "!**/.git",
    "!**/.git/**",
    "!**/.qwenignore",
    "!**/.agentignore",
    "!**/.aiignore",
)
CUSTOM_IGNORE_NAMES = (".qwenignore", ".agentignore", ".aiignore")


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


def build_command(
    args: argparse.Namespace, root: Path, paths: list[str], *, follow: bool = False
) -> list[str]:
    result = ["rg", "--hidden", "--no-ignore-parent", "--sort", "path"]
    if follow:
        result.append("--follow")
    for ignore_file in custom_ignore_files(root):
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


def custom_ignore_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for ignore_name in CUSTOM_IGNORE_NAMES:
        ignore_file = root / ignore_name
        if not ignore_file.exists():
            continue
        if ignore_file.is_symlink() or not ignore_file.is_file():
            raise SearchError("custom ignore controls must be regular files")
        files.append(ignore_file)
    return files


def selected_by_globs(patterns: list[str], value: str) -> bool:
    path = PurePosixPath(value)
    selected = not any(not pattern.startswith("!") for pattern in patterns)
    for raw_pattern in patterns:
        excluded = raw_pattern.startswith("!")
        pattern = raw_pattern[1:] if excluded else raw_pattern
        if path.match(pattern):
            selected = not excluded
    return selected


def normalized_path(value: str) -> str:
    return value.removeprefix("./")


def git_ignored_paths(root: Path, values: list[str]) -> set[str]:
    if not values:
        return set()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--no-index", "-z", "--stdin"],
            input=b"\0".join(value.encode() for value in values) + b"\0",
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise SearchError("tracked symlink ignore rules could not be evaluated") from exc
    if result.returncode not in (0, 1):
        raise SearchError("tracked symlink ignore rules could not be evaluated")
    return {value.decode("utf-8", "replace") for value in result.stdout.split(b"\0") if value}


def native_ignore_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["rg", "--files", "--hidden", "--no-ignore", "--glob", ".ignore", "."],
            cwd=root,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise SearchError("worktree ignore controls could not be enumerated") from exc
    if result.returncode not in (0, 1):
        raise SearchError("worktree ignore controls could not be enumerated")
    paths = [
        root / normalized_path(value.decode("utf-8", "replace"))
        for value in result.stdout.splitlines()
        if value
    ]
    if len(paths) > MAX_IGNORE_FILES:
        raise SearchError("worktree ignore control count exceeds 10000")
    files: list[Path] = []
    for path in paths:
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if private_path(relative.parent):
            continue
        if path.is_symlink() or not path.is_file():
            raise SearchError("worktree ignore controls must be regular files")
        files.append(path)
    return files


def ripgrep_visible_paths(root: Path, values: list[str]) -> set[str]:
    wanted = set(values)
    custom_files = custom_ignore_files(root)
    native_files = native_ignore_files(root)
    if not custom_files and not native_files:
        return wanted
    try:
        with tempfile.TemporaryDirectory(prefix="readonly-search-ignore-") as directory:
            mirror = Path(directory)
            for value in wanted:
                placeholder = mirror / value
                placeholder.parent.mkdir(parents=True, exist_ok=True)
                placeholder.touch()
            for source in native_files:
                destination = mirror / source.relative_to(root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            mirrored_custom: list[Path] = []
            for source in custom_files:
                destination = mirror / source.name
                shutil.copyfile(source, destination)
                mirrored_custom.append(destination)
            command = [
                "rg",
                "--files",
                "--hidden",
                "--no-ignore-vcs",
                "--no-ignore-parent",
                "--no-ignore-global",
                "--sort",
                "path",
            ]
            for ignore_file in mirrored_custom:
                command.extend(("--ignore-file", str(ignore_file)))
            command.append(".")
            result = subprocess.run(
                command,
                cwd=mirror,
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
            )
            if result.returncode not in (0, 1):
                raise SearchError("symlink ignore rules could not be evaluated")
            return {
                normalized_path(value.decode("utf-8", "replace"))
                for value in result.stdout.splitlines()
                if normalized_path(value.decode("utf-8", "replace")) in wanted
            }
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SearchError("symlink ignore rules could not be evaluated") from exc


def run(
    args: argparse.Namespace, root: Path, paths: list[str], *, follow: bool = False
) -> tuple[list[Any], bool]:
    try:
        process = subprocess.Popen(
            build_command(args, root, paths, follow=follow),
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


def tracked_symlinks(
    root: Path, paths: list[str], globs: list[str]
) -> tuple[list[str], int, int, int]:
    try:
        process = subprocess.Popen(
            ["git", "-C", str(root), "ls-files", "-s", "-z", "--", *paths],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise SearchError("tracked symlinks could not be enumerated") from exc
    timed_out = False

    def expire() -> None:
        nonlocal timed_out
        timed_out = True
        process.kill()

    timer = threading.Timer(TIMEOUT_SECONDS, expire)
    timer.start()
    links: list[str] = []
    buffer = b""
    assert process.stdout is not None
    try:
        while chunk := process.stdout.read(64 * 1024):
            buffer += chunk
            entries = buffer.split(b"\0")
            buffer = entries.pop()
            for entry in entries:
                metadata, separator, raw_path = entry.partition(b"\t")
                if not separator or not metadata.startswith(b"120000 "):
                    continue
                links.append(raw_path.decode("utf-8", "replace"))
                if len(links) > MAX_TRACKED_SYMLINKS:
                    process.terminate()
                    process.wait()
                    raise SearchError("tracked symlink scan exceeds 10000; narrow --path")
        returncode = process.wait()
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
    if timed_out or returncode != 0:
        raise SearchError("tracked symlinks could not be enumerated")

    resolved: list[tuple[str, str]] = []
    skipped_unsafe = 0
    for value in links:
        path = PurePosixPath(value)
        link = root / value
        try:
            target = link.resolve(strict=True)
            relative_target = PurePosixPath(target.relative_to(root).as_posix())
        except (OSError, ValueError):
            skipped_unsafe += 1
            continue
        if private_path(path) or private_path(relative_target) or not target.is_file():
            skipped_unsafe += 1
            continue
        resolved.append((value, relative_target.as_posix()))

    ignored = git_ignored_paths(root, [item for pair in resolved for item in pair])
    visible = ripgrep_visible_paths(root, [item for pair in resolved for item in pair])
    safe: list[str] = []
    skipped_ignored = 0
    skipped_filtered = 0
    for value, relative_target in resolved:
        if (
            value in ignored
            or relative_target in ignored
            or value not in visible
            or relative_target not in visible
        ):
            skipped_ignored += 1
            continue
        if not selected_by_globs(globs, value):
            skipped_filtered += 1
            continue
        safe.append(value)
    return safe, skipped_unsafe, skipped_ignored, skipped_filtered


def merged_results(
    args: argparse.Namespace, root: Path, paths: list[str]
) -> tuple[list[Any], bool, dict[str, int]]:
    items, truncated = run(args, root, paths)
    links, skipped_unsafe, skipped_ignored, skipped_filtered = tracked_symlinks(
        root, paths, args.glob
    )
    if links:
        link_items, link_truncated = run(args, root, links, follow=True)
        items.extend(link_items)
        truncated = truncated or link_truncated
    if args.operation == "files":
        items = sorted(set(items))
    else:
        unique = {
            (item["path"], item["line"], item["text"]): item
            for item in items
            if isinstance(item, dict)
        }
        items = sorted(unique.values(), key=lambda item: (item["path"], item["line"], item["text"]))
    wanted = args.offset + args.limit + 1
    return (
        items[:wanted],
        truncated or len(items) > wanted,
        {
            "safe_tracked": len(links),
            "skipped_unsafe": skipped_unsafe,
            "skipped_ignored": skipped_ignored,
            "skipped_filtered": skipped_filtered,
        },
    )


def main() -> int:
    try:
        args = parser().parse_args()
        root, paths = validate(args)
        all_items, stopped_early, symlink_coverage = merged_results(args, root, paths)
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
                    "symlink_coverage": symlink_coverage,
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
