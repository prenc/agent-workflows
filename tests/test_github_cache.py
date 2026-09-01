from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "src/github_workflows/github_cache.py"
REPO = "example/private-repo"


class TestGithubCache:
    def setup_method(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp.name) / "qwen-project"

    def teardown_method(self) -> None:
        self.temp.cleanup()

    def call(self, *args: str, check: bool = True):
        return subprocess.run([str(SCRIPT), *args], check=check, capture_output=True, text=True)

    def parsed(self, *args: str) -> dict:
        return json.loads(self.call(*args).stdout)

    def common(self) -> list[str]:
        return ["--repo", REPO, "--project-dir", str(self.project_dir)]

    def test_records_database_is_project_local(self) -> None:
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        payload = Path(self.temp.name) / "records.json"
        payload.write_text(
            json.dumps({"records": [{"number": 1, "state": "open", "title": "Parser"}]})
        )
        self.parsed(
            "ingest-records",
            *self.common(),
            "--db",
            prepared["work_db"],
            "--kind",
            "issue",
            "--input",
            str(payload),
            "--source",
            "test",
        )
        committed = self.parsed(
            "commit-records",
            *self.common(),
            "--run-id",
            "first",
            "--db",
            prepared["work_db"],
            "--base-generation",
            "0",
            "--synced-at",
            "2026-08-30T00:00:00Z",
            "--default-sha",
            "abc",
            "--full-history-complete",
        )
        expected = self.project_dir / "github" / "records-v1.sqlite3"
        assert Path(committed["committed"]) == expected
        queried = self.parsed(
            "query-records", *self.common(), "--db", str(expected), "--terms", "Parser"
        )
        assert queried["records"][0]["number"] == 1

    def test_generation_conflict_preserves_live_database(self) -> None:
        left = self.parsed("prepare-records", *self.common(), "--run-id", "left")
        right = self.parsed("prepare-records", *self.common(), "--run-id", "right")
        base = [
            "--base-generation",
            "0",
            "--synced-at",
            "2026-08-30T00:00:00Z",
            "--default-sha",
            "abc",
        ]
        self.parsed(
            "commit-records", *self.common(), "--run-id", "left", "--db", left["work_db"], *base
        )
        failed = self.call(
            "commit-records",
            *self.common(),
            "--run-id",
            "right",
            "--db",
            right["work_db"],
            *base,
            check=False,
        )
        assert failed.returncode == 2
        assert "generation conflict" in failed.stderr

    def test_project_directory_is_required(self) -> None:
        failed = self.call("status", "--repo", REPO, check=False)
        assert failed.returncode == 2
        assert "QWEN_CODE_PROJECT_DIR" in failed.stderr
