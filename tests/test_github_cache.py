from __future__ import annotations

import json
import sqlite3
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
            json.dumps(
                {
                    "records": [
                        {
                            "number": 1,
                            "state": "open",
                            "title": "Parser",
                            "body": "body-only-secret-term",
                            "labels": ["bug"],
                            "comments": [{"body": "detail"}],
                        }
                    ]
                }
            )
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
        record = queried["records"][0]
        assert record["number"] == 1
        assert record["summary"] == "Parser [labels: bug]"
        assert "body" not in record
        body_only = self.parsed(
            "query-records",
            *self.common(),
            "--db",
            str(expected),
            "--terms",
            "body-only-secret-term",
        )
        assert body_only["records"] == []
        with sqlite3.connect(expected) as connection:
            stored = connection.execute(
                "SELECT body, comments_json, hydration FROM records WHERE number=1"
            ).fetchone()
        assert stored == ("", "[]", "summary")

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

    def test_reused_database_compacts_unrefreshed_legacy_records(self) -> None:
        first = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        self.parsed(
            "commit-records",
            *self.common(),
            "--run-id",
            "first",
            "--db",
            first["work_db"],
            "--base-generation",
            "0",
            "--synced-at",
            "2026-08-30T00:00:00Z",
            "--default-sha",
            "abc",
        )
        live = self.project_dir / "github" / "records-v1.sqlite3"
        with sqlite3.connect(live) as connection:
            connection.execute(
                """INSERT INTO records (
                    kind, number, state, title, body, comments_json, labels_json,
                    assignees_json, relationships_json, commits_json, content_sha256,
                    hydration, source, fetched_at
                ) VALUES ('issue', 9, 'closed', 'Legacy', 'secret', '[{\"body\":\"secret\"}]',
                    '[\"old\"]', '[]', '{\"pull\":1}', '[{\"sha\":\"abc\"}]', 'old',
                    'detail', 'legacy', '2024-01-01T00:00:00Z')"""
            )
            connection.execute(
                "INSERT INTO records_fts(kind, number, title, body, labels) "
                "VALUES ('issue', 9, 'Legacy', 'secret', '[\"old\"]')"
            )
        reused = self.parsed("prepare-records", *self.common(), "--run-id", "second")
        with sqlite3.connect(reused["work_db"]) as connection:
            stored = connection.execute(
                "SELECT body, comments_json, relationships_json, commits_json, hydration "
                "FROM records WHERE number=9"
            ).fetchone()
            body_matches = connection.execute(
                "SELECT count(*) FROM records_fts WHERE records_fts MATCH 'body : secret'"
            ).fetchone()[0]
        assert stored == ("", "[]", "{}", "[]", "summary")
        assert body_matches == 0

    def test_project_directory_is_required(self) -> None:
        failed = self.call("status", "--repo", REPO, check=False)
        assert failed.returncode == 2
        assert "QWEN_CODE_PROJECT_DIR" in failed.stderr
