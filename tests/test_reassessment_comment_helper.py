from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest

ROOT = Path(__file__).parents[1]
HELPER = ROOT / "codex/skills/gh-pickup-work/scripts/update_managed_comment.py"


def load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reassessment_comment_helper", HELPER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def completed(payload: object | None = None) -> subprocess.CompletedProcess[str]:
    stdout = "" if payload is None else json.dumps(payload)
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


@pytest.mark.parametrize(
    "marker",
    [
        "<!-- codex:github-work-reassessment:v1 -->",
        "<!-- codex:github-issue-reevaluation:v1 -->",
    ],
)
def test_delete_comment_verifies_owner_and_marker(marker: str) -> None:
    helper = load_helper()
    responses = [
        completed(
            {
                "body": f"{marker}\nFinding",
                "html_url": "https://example.invalid/comment/7",
                "issue_url": "https://api.github.com/repos/owner/repo/issues/12",
                "user": {"login": "maintainer"},
            }
        ),
        completed({"login": "maintainer"}),
        completed(),
    ]

    with mock.patch.object(helper.subprocess, "run", side_effect=responses) as run:
        comment = helper.delete_comment("Owner/Repo", 7, 12)

    assert comment["html_url"] == "https://example.invalid/comment/7"
    assert run.call_args_list[-1].args[0] == [
        "gh",
        "api",
        "--method",
        "DELETE",
        "repos/Owner/Repo/issues/comments/7",
    ]


@pytest.mark.parametrize(
    ("comment", "user", "message"),
    [
        (
            {
                "body": "<!-- codex:github-work-reassessment:v1 -->\nFinding",
                "issue_url": "https://api.github.com/repos/owner/repo/issues/12",
                "user": {"login": "someone-else"},
            },
            {"login": "maintainer"},
            "not owned",
        ),
        (
            {
                "body": "Ordinary comment",
                "issue_url": "https://api.github.com/repos/owner/repo/issues/12",
                "user": {"login": "maintainer"},
            },
            {"login": "maintainer"},
            "recognized managed marker",
        ),
    ],
)
def test_delete_comment_rejects_unmanaged_targets(
    comment: dict[str, object], user: dict[str, str], message: str
) -> None:
    helper = load_helper()

    with (
        mock.patch.object(
            helper.subprocess, "run", side_effect=[completed(comment), completed(user)]
        ),
        pytest.raises(helper.UpdateError, match=message),
    ):
        helper.delete_comment("owner/repo", 7, 12)


def test_delete_comment_rejects_a_different_artifact() -> None:
    helper = load_helper()
    comment = {
        "body": "<!-- codex:github-work-reassessment:v1 -->\nFinding",
        "issue_url": "https://api.github.com/repos/owner/repo/issues/99",
        "user": {"login": "maintainer"},
    }

    with (
        mock.patch.object(
            helper.subprocess,
            "run",
            side_effect=[completed(comment), completed({"login": "maintainer"})],
        ),
        pytest.raises(helper.UpdateError, match="expected artifact"),
    ):
        helper.delete_comment("owner/repo", 7, 12)


def test_update_comment_verifies_owner_and_marker() -> None:
    helper = load_helper()
    body = "<!-- codex:github-work-reassessment:v1 -->\nFinding\n"
    responses = [
        completed(
            {
                "body": "<!-- codex:github-work-reassessment:v1 -->\nFinding",
                "html_url": "https://example.invalid/comment/7",
                "issue_url": "https://api.github.com/repos/owner/repo/issues/12",
                "user": {"login": "maintainer"},
            }
        ),
        completed({"login": "maintainer"}),
        completed({"id": 7, "html_url": "https://example.invalid/comment/7"}),
    ]

    with mock.patch.object(helper.subprocess, "run", side_effect=responses) as run:
        comment = helper.update_managed_comment("Owner/Repo", 7, 12, body)

    assert comment["id"] == 7
    assert run.call_args_list[-1].args[0] == [
        "gh",
        "api",
        "--method",
        "PATCH",
        "repos/Owner/Repo/issues/comments/7",
        "--input",
        "-",
    ]
    assert run.call_args_list[-1].kwargs["input"] == json.dumps({"body": body})


@pytest.mark.parametrize(
    ("comment", "user", "message"),
    [
        (
            {
                "body": "<!-- codex:github-work-reassessment:v1 -->\nFinding",
                "issue_url": "https://api.github.com/repos/owner/repo/issues/12",
                "user": {"login": "someone-else"},
            },
            {"login": "maintainer"},
            "not owned",
        ),
        (
            {
                "body": "Ordinary comment",
                "issue_url": "https://api.github.com/repos/owner/repo/issues/12",
                "user": {"login": "maintainer"},
            },
            {"login": "maintainer"},
            "recognized managed marker",
        ),
        (
            {
                "body": "<!-- codex:github-work-reassessment:v1 -->\nFinding",
                "issue_url": "https://api.github.com/repos/owner/repo/issues/99",
                "user": {"login": "maintainer"},
            },
            {"login": "maintainer"},
            "expected artifact",
        ),
    ],
)
def test_update_comment_rejects_unverified_targets(
    comment: dict[str, object], user: dict[str, str], message: str
) -> None:
    helper = load_helper()
    body = "<!-- codex:github-work-reassessment:v1 -->\nFinding\n"

    with (
        mock.patch.object(
            helper.subprocess, "run", side_effect=[completed(comment), completed(user)]
        ) as run,
        pytest.raises(helper.UpdateError, match=message),
    ):
        helper.update_managed_comment("owner/repo", 7, 12, body)

    assert all("PATCH" not in call.args[0] for call in run.call_args_list)


def test_update_dry_run_verifies_the_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    helper = load_helper()
    body_file = tmp_path / "comment.md"
    body_file.write_text("<!-- codex:github-work-reassessment:v1 -->\nFinding\n")
    monkeypatch.setattr(
        helper.sys,
        "argv",
        [
            "update_managed_comment.py",
            "--repo",
            "owner/repo",
            "--comment-id",
            "7",
            "--artifact-number",
            "12",
            "--body-file",
            str(body_file),
            "--dry-run",
        ],
    )
    responses = [
        completed(
            {
                "body": "<!-- codex:github-work-reassessment:v1 -->\nFinding",
                "issue_url": "https://api.github.com/repos/owner/repo/issues/12",
                "user": {"login": "maintainer"},
            }
        ),
        completed({"login": "maintainer"}),
    ]

    with mock.patch.object(helper.subprocess, "run", side_effect=responses) as run:
        assert helper.main() == 0

    assert json.loads(capsys.readouterr().out) == {"action": "would-update", "comment_id": 7}
    assert all("PATCH" not in call.args[0] for call in run.call_args_list)


def test_update_dry_run_rejects_an_unverified_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    body_file = tmp_path / "comment.md"
    body_file.write_text("<!-- codex:github-work-reassessment:v1 -->\nFinding\n")
    monkeypatch.setattr(
        helper.sys,
        "argv",
        [
            "update_managed_comment.py",
            "--repo",
            "owner/repo",
            "--comment-id",
            "7",
            "--artifact-number",
            "12",
            "--body-file",
            str(body_file),
            "--dry-run",
        ],
    )
    comment = {
        "body": "<!-- codex:github-work-reassessment:v1 -->\nFinding",
        "issue_url": "https://api.github.com/repos/owner/repo/issues/12",
        "user": {"login": "someone-else"},
    }

    with (
        mock.patch.object(
            helper.subprocess,
            "run",
            side_effect=[completed(comment), completed({"login": "maintainer"})],
        ) as run,
        pytest.raises(helper.UpdateError, match="not owned"),
    ):
        helper.main()

    assert all("PATCH" not in call.args[0] for call in run.call_args_list)


def test_update_requires_an_artifact_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    body_file = tmp_path / "comment.md"
    body_file.write_text("<!-- codex:github-work-reassessment:v1 -->\nFinding\n")
    monkeypatch.setattr(
        helper.sys,
        "argv",
        [
            "update_managed_comment.py",
            "--repo",
            "owner/repo",
            "--comment-id",
            "7",
            "--body-file",
            str(body_file),
        ],
    )

    with pytest.raises(helper.UpdateError, match="artifact-number"):
        helper.main()


def test_run_gh_converts_a_timeout_to_update_error() -> None:
    helper = load_helper()

    with (
        mock.patch.object(
            helper.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("gh", helper.GH_TIMEOUT_SECONDS),
        ) as run,
        pytest.raises(helper.UpdateError, match="timed out"),
    ):
        helper.run_gh(["gh", "api", "user"])

    assert run.call_args.kwargs["timeout"] == helper.GH_TIMEOUT_SECONDS
