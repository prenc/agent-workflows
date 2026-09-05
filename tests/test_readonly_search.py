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
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True
    )
    (repository / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    (repository / "visible.py").write_text("needle one\nneedle two\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Initial"], cwd=repository, check=True)
    worktree = repository / ".worktrees/gh-audit-repo-aaaaaaa"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
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


def test_custom_ignore_negation_does_not_filter_unrelated_files(ignored_worktree: Path) -> None:
    (ignored_worktree / ".qwenignore").write_text("drop.log\n!keep.log\n", encoding="utf-8")
    (ignored_worktree / "drop.log").write_text("drop\n", encoding="utf-8")
    (ignored_worktree / "keep.log").write_text("keep\n", encoding="utf-8")

    result = invoke("files", "--root", str(ignored_worktree))

    assert result.returncode == 0
    assert json.loads(result.stdout)["items"] == ["keep.log", "visible.py"]


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


def test_search_includes_only_safe_tracked_file_symlinks(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("needle outside\n", encoding="utf-8")
    (repository / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    (repository / "target.md").write_text("needle target\n", encoding="utf-8")
    (repository / "docs").mkdir()
    (repository / "docs/other.md").write_text("other\n", encoding="utf-8")
    (repository / "linked.md").symlink_to("target.md")
    (repository / "external.md").symlink_to(outside)
    (repository / "docs-link").symlink_to("docs", target_is_directory=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Symlinks"], cwd=repository, check=True)
    worktree = repository / ".worktrees/gh-audit-repo-bbbbbbb"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
    )

    result = invoke("search", "--root", str(worktree), "--pattern", "needle")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert [(item["path"], item["text"]) for item in payload["items"]] == [
        ("linked.md", "needle target"),
        ("target.md", "needle target"),
    ]
    assert payload["symlink_coverage"] == {
        "safe_tracked": 1,
        "skipped_unsafe": 2,
        "skipped_ignored": 0,
        "skipped_filtered": 0,
    }

    limited = invoke("search", "--root", str(worktree), "--pattern", "needle", "--limit", "1")
    limited_payload = json.loads(limited.stdout)
    assert limited_payload["truncated"] is True
    assert limited_payload["next_offset"] == 1

    (worktree / ".qwenignore").write_text("linked.md\n", encoding="utf-8")
    ignored = invoke("search", "--root", str(worktree), "--pattern", "needle")
    ignored_payload = json.loads(ignored.stdout)
    assert [item["path"] for item in ignored_payload["items"]] == ["target.md"]
    assert ignored_payload["symlink_coverage"] == {
        "safe_tracked": 0,
        "skipped_unsafe": 2,
        "skipped_ignored": 1,
        "skipped_filtered": 0,
    }

    (worktree / ".qwenignore").write_text("target.md\n", encoding="utf-8")
    ignored_target = invoke("search", "--root", str(worktree), "--pattern", "needle")
    ignored_target_payload = json.loads(ignored_target.stdout)
    assert ignored_target_payload["items"] == []
    assert ignored_target_payload["symlink_coverage"]["skipped_ignored"] == 1

    (worktree / ".qwenignore").write_text("", encoding="utf-8")
    filtered = invoke(
        "search",
        "--root",
        str(worktree),
        "--pattern",
        "needle",
        "--glob",
        "*.py",
    )
    filtered_payload = json.loads(filtered.stdout)
    assert filtered_payload["items"] == []
    assert filtered_payload["symlink_coverage"] == {
        "safe_tracked": 0,
        "skipped_unsafe": 2,
        "skipped_ignored": 0,
        "skipped_filtered": 1,
    }


@pytest.mark.parametrize(
    ("target_name", "ignore_rules"),
    [
        ("nested/secret/deep/file.txt", "secret/\n"),
        ("!secret.txt", "\\!secret.txt\n"),
        ("blocked/keep/file.txt", "blocked/\n!blocked/keep/file.txt\n"),
    ],
)
def test_symlink_target_preserves_ripgrep_ignore_semantics(
    ignored_worktree: Path, target_name: str, ignore_rules: str
) -> None:
    target = ignored_worktree / target_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("needle private\n", encoding="utf-8")
    (ignored_worktree / "leak.txt").symlink_to(target_name)
    subprocess.run(
        ["git", "add", target_name, "leak.txt"],
        cwd=ignored_worktree,
        check=True,
    )
    (ignored_worktree / ".qwenignore").write_text(ignore_rules, encoding="utf-8")

    result = invoke("search", "--root", str(ignored_worktree), "--pattern", "needle")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert [item["path"] for item in payload["items"]] == ["visible.py", "visible.py"]
    assert payload["symlink_coverage"] == {
        "safe_tracked": 0,
        "skipped_unsafe": 0,
        "skipped_ignored": 1,
        "skipped_filtered": 0,
    }


def test_symlink_target_respects_nested_native_ignore(ignored_worktree: Path) -> None:
    target = ignored_worktree / "nested/secret/deep/file.txt"
    target.parent.mkdir(parents=True)
    target.write_text("needle private\n", encoding="utf-8")
    (ignored_worktree / "nested/.ignore").write_text("secret/\n", encoding="utf-8")
    (ignored_worktree / "leak.txt").symlink_to("nested/secret/deep/file.txt")
    subprocess.run(
        ["git", "add", "nested/.ignore", "nested/secret/deep/file.txt", "leak.txt"],
        cwd=ignored_worktree,
        check=True,
    )

    result = invoke("search", "--root", str(ignored_worktree), "--pattern", "needle")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert [item["path"] for item in payload["items"]] == ["visible.py", "visible.py"]
    assert payload["symlink_coverage"]["skipped_ignored"] == 1
