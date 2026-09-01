#!/usr/bin/env python3
"""Update one already-resolved managed GitHub issue or PR conversation comment."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

MARKER = "<!-- codex:github-work-reassessment:v1 -->"
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
MAX_BODY_BYTES = 128 * 1024


class UpdateError(RuntimeError):
    """A validation or GitHub update failed safely."""


def load_body(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UpdateError(f"cannot read body file {path}: {exc}") from exc
    if len(raw) > MAX_BODY_BYTES:
        raise UpdateError(f"comment body exceeds {MAX_BODY_BYTES} bytes")
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateError("comment body must be UTF-8") from exc
    if body.count(MARKER) != 1 or body.splitlines()[:1] != [MARKER]:
        raise UpdateError(
            "comment body must contain the managed marker exactly once as its first line"
        )
    return body.rstrip() + "\n"


def emit(action: str, *, url: str | None = None, comment_id: int) -> None:
    result: dict[str, Any] = {"action": action, "comment_id": comment_id}
    if url is not None:
        result["url"] = url
    print(json.dumps(result, sort_keys=True))


def update_comment(repo: str, comment_id: int, body: str) -> dict[str, Any]:
    command = [
        "gh",
        "api",
        "--method",
        "PATCH",
        f"repos/{repo}/issues/comments/{comment_id}",
        "--input",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            input=json.dumps({"body": body}),
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UpdateError("gh is not installed or is not on PATH") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown gh error"
        raise UpdateError(f"gh api update-comment failed: {detail}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise UpdateError("gh returned invalid JSON for updated comment") from exc
    if not isinstance(value, dict):
        raise UpdateError("updated comment response is not an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub repository as OWNER/REPO")
    parser.add_argument("--comment-id", required=True, type=int, help="Existing comment ID")
    parser.add_argument("--body-file", required=True, type=Path, help="UTF-8 Markdown body")
    parser.add_argument("--dry-run", action="store_true", help="Validate without mutation")
    args = parser.parse_args()

    if not REPOSITORY_RE.fullmatch(args.repo):
        raise UpdateError("--repo must be OWNER/REPO using GitHub-safe name characters")
    if args.comment_id <= 0:
        raise UpdateError("--comment-id must be positive")
    body = load_body(args.body_file)

    if args.dry_run:
        emit("would-update", comment_id=args.comment_id)
        return 0

    updated = update_comment(args.repo, args.comment_id, body)
    returned_id = updated.get("id")
    if returned_id != args.comment_id:
        raise UpdateError("updated comment response has an unexpected comment ID")
    url = updated.get("html_url") if isinstance(updated.get("html_url"), str) else None
    emit("updated", url=url, comment_id=args.comment_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
