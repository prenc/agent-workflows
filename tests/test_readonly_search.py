from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SEARCH = ROOT / "extensions/github-workflows/hooks/readonly-search.py"


def invoke(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SEARCH), *arguments],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.fixture
def ignored_worktree(tmp_path: Path) -> Path:
    (tmp_path / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    worktree = tmp_path / ".worktrees/gh-audit-repo-aaaaaaa"
    worktree.mkdir(parents=True)
    git_dir = tmp_path / ".git/worktrees/gh-audit-repo-aaaaaaa"
    git_dir.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    (worktree / "visible.py").write_text("needle one\nneedle two\n", encoding="utf-8")
    return worktree


def test_search_bypasses_only_parent_ignore_and_paginates(ignored_worktree: Path) -> None:
    result = invoke(
        "search",
        "--root",
        str(ignored_worktree),
        "--pattern",
        "needle",
        "--limit",
        "1",
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["items"] == [{"path": "visible.py", "line": 1, "text": "needle one"}]
    assert payload["truncated"] is True
    assert payload["next_offset"] == 1


def test_files_respects_local_and_private_exclusions(ignored_worktree: Path) -> None:
    (ignored_worktree / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (ignored_worktree / ".qwenignore").write_text("qwen.txt\n", encoding="utf-8")
    (ignored_worktree / "ignored.txt").write_text("hidden", encoding="utf-8")
    (ignored_worktree / "qwen.txt").write_text("hidden", encoding="utf-8")
    (ignored_worktree / ".env").write_text("secret", encoding="utf-8")
    (ignored_worktree / "data").mkdir()
    (ignored_worktree / "data/record.txt").write_text("private", encoding="utf-8")

    result = invoke("files", "--root", str(ignored_worktree))

    assert result.returncode == 0
    assert json.loads(result.stdout)["items"] == ["visible.py"]


def test_search_rejects_escape_and_private_storage(tmp_path: Path) -> None:
    root = tmp_path / "gh-audit-repo-aaaaaaa"
    root.mkdir()
    git_dir = tmp_path / ".git/worktrees/gh-audit-repo-aaaaaaa"
    git_dir.mkdir(parents=True)
    (root / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")

    escape = invoke("files", "--root", str(root), "--path", "../outside")
    assert escape.returncode == 2
    assert "relative path" in json.loads(escape.stdout)["error"]

    environment = {"QWEN_CODE_PROJECT_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    private = invoke("files", "--root", str(root), env=environment)
    assert private.returncode == 2
    assert "private workflow storage" in json.loads(private.stdout)["error"]


def test_invalid_requests_return_structured_errors(ignored_worktree: Path) -> None:
    missing_pattern = invoke("search", "--root", str(ignored_worktree))
    assert missing_pattern.returncode == 2
    assert "error" in json.loads(missing_pattern.stdout)

    invalid_regex = invoke("search", "--root", str(ignored_worktree), "--pattern", "[unterminated")
    assert invalid_regex.returncode == 2
    assert json.loads(invalid_regex.stdout) == {"error": "ripgrep rejected the search request"}
