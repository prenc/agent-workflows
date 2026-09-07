from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest

ROOT = Path(__file__).parents[1]
HELPER = ROOT / "codex/skills/gh-reassess-work/scripts/update_managed_comment.py"


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
