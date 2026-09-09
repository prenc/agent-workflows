#!/usr/bin/env python3
"""Update or delete one already-resolved managed GitHub conversation comment."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MARKER = "<!-- codex:github-work-reassessment:v1 -->"
LEGACY_MARKER = "<!-- codex:github-issue-reevaluation:v1 -->"
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
MAX_BODY_BYTES = 128 * 1024
GH_TIMEOUT_SECONDS = 60


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


def run_gh(
    command: list[str], *, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=GH_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise UpdateError("gh is not installed or is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"gh api request timed out after {GH_TIMEOUT_SECONDS} seconds") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown gh error"
        raise UpdateError(f"gh api request failed: {detail}")
    return result


def parse_object(raw: str, *, description: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UpdateError(f"gh returned invalid JSON for {description}") from exc
    if not isinstance(value, dict):
        raise UpdateError(f"gh returned a non-object for {description}")
    return value


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
    result = run_gh(command, input_text=json.dumps({"body": body}))
    return parse_object(result.stdout, description="updated comment")


def resolve_owned_managed_comment(
    repo: str, comment_id: int, artifact_number: int
) -> dict[str, Any]:
    comment_result = run_gh(["gh", "api", f"repos/{repo}/issues/comments/{comment_id}"])
    user_result = run_gh(["gh", "api", "user"])
    comment = parse_object(comment_result.stdout, description="managed comment")
    user = parse_object(user_result.stdout, description="authenticated user")

    owner = comment.get("user")
    owner_login = owner.get("login") if isinstance(owner, dict) else None
    authenticated_login = user.get("login")
    if (
        not isinstance(owner_login, str)
        or not isinstance(authenticated_login, str)
        or owner_login.casefold() != authenticated_login.casefold()
    ):
        raise UpdateError("managed comment is not owned by the authenticated user")

    body = comment.get("body")
    first_line = body.splitlines()[:1] if isinstance(body, str) else []
    if first_line not in ([MARKER], [LEGACY_MARKER]):
        raise UpdateError("comment does not begin with a recognized managed marker")
    if sum(body.count(marker) for marker in (MARKER, LEGACY_MARKER)) != 1:
        raise UpdateError("comment must contain exactly one recognized managed marker")

    issue_url = comment.get("issue_url")
    repo_owner, repo_name = repo.split("/", 1)
    path_parts = (
        urlparse(issue_url).path.strip("/").split("/") if isinstance(issue_url, str) else []
    )
    expected_parts = ["repos", repo_owner, repo_name, "issues", str(artifact_number)]
    if len(path_parts) < len(expected_parts) or [
        part.casefold() for part in path_parts[-len(expected_parts) :]
    ] != [part.casefold() for part in expected_parts]:
        raise UpdateError("managed comment does not belong to the expected artifact")
    return comment


def update_managed_comment(
    repo: str, comment_id: int, artifact_number: int, body: str
) -> dict[str, Any]:
    resolve_owned_managed_comment(repo, comment_id, artifact_number)
    return update_comment(repo, comment_id, body)


def delete_comment(repo: str, comment_id: int, artifact_number: int) -> dict[str, Any]:
    comment = resolve_owned_managed_comment(repo, comment_id, artifact_number)
    run_gh(["gh", "api", "--method", "DELETE", f"repos/{repo}/issues/comments/{comment_id}"])
    return comment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub repository as OWNER/REPO")
    parser.add_argument("--comment-id", required=True, type=int, help="Existing comment ID")
    parser.add_argument("--artifact-number", type=int, help="Expected issue or PR number")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--body-file", type=Path, help="UTF-8 Markdown update body")
    action.add_argument("--delete", action="store_true", help="Delete the managed comment")
    parser.add_argument("--dry-run", action="store_true", help="Validate without mutation")
    args = parser.parse_args()

    if not REPOSITORY_RE.fullmatch(args.repo):
        raise UpdateError("--repo must be OWNER/REPO using GitHub-safe name characters")
    if args.comment_id <= 0:
        raise UpdateError("--comment-id must be positive")
    if args.delete:
        if args.artifact_number is None or args.artifact_number <= 0:
            raise UpdateError("--artifact-number must be positive for deletion")
        if args.dry_run:
            comment = resolve_owned_managed_comment(
                args.repo, args.comment_id, args.artifact_number
            )
            url = comment.get("html_url") if isinstance(comment.get("html_url"), str) else None
            emit("would-delete", url=url, comment_id=args.comment_id)
            return 0
        comment = delete_comment(args.repo, args.comment_id, args.artifact_number)
        url = comment.get("html_url") if isinstance(comment.get("html_url"), str) else None
        emit("deleted", url=url, comment_id=args.comment_id)
        return 0

    if args.body_file is None:
        raise UpdateError("--body-file is required for update")
    if args.artifact_number is None or args.artifact_number <= 0:
        raise UpdateError("--artifact-number must be positive for update")
    body = load_body(args.body_file)
    if args.dry_run:
        resolve_owned_managed_comment(args.repo, args.comment_id, args.artifact_number)
        emit("would-update", comment_id=args.comment_id)
        return 0

    updated = update_managed_comment(args.repo, args.comment_id, args.artifact_number, body)
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
