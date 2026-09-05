from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from github_workflows.github_cache import RECORDS_INPUT_BYTES

SCRIPT = Path(__file__).parents[1] / "src/github_workflows/github_cache.py"
REPO = "example/private-repo"


class TestGithubCache:
    def setup_method(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp.name) / "qwen-project"

    def teardown_method(self) -> None:
        self.temp.cleanup()

    def live_db(self, repo: str = REPO) -> Path:
        owner, name = repo.split("/")
        return self.project_dir / "github" / owner / name / "records-v1.sqlite3"

    def call(
        self, *args: str, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        clean_env = {
            key: value for key, value in os.environ.items() if key != "QWEN_CODE_PROJECT_DIR"
        }
        if env is not None:
            clean_env.update(env)
        return subprocess.run(
            [str(SCRIPT), *args],
            check=check,
            capture_output=True,
            text=True,
            env=clean_env,
        )

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
            "--run-id",
            "first",
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
        expected = self.live_db()
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
        live = self.live_db()
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

    def failed_call(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.call(*args, check=False)

    def ingest_args(
        self, work_db: str, run_id: str = "first", records: list[dict[str, Any]] | None = None
    ) -> list[str]:
        payload = Path(self.temp.name) / "records.json"
        if records is None:
            records = [{"number": 1, "state": "open", "title": "Scoped"}]
        payload.write_text(json.dumps({"records": records}))
        return [
            "ingest-records",
            *self.common(),
            "--run-id",
            run_id,
            "--db",
            work_db,
            "--kind",
            "issue",
            "--input",
            str(payload),
            "--source",
            "test",
        ]

    def test_failed_ingest_does_not_wedge_next_prepare(self) -> None:
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        work_db = Path(prepared["work_db"])
        work_db.unlink()
        failed = self.failed_call(*self.ingest_args(str(work_db)))
        assert failed.returncode == 2
        assert "does not exist" in failed.stderr
        assert not work_db.exists()
        retried = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        assert Path(retried["work_db"]) == work_db

    def test_ingest_rejects_database_outside_prepared_staging(self) -> None:
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        work_db = prepared["work_db"]
        live = self.live_db()
        failed = self.failed_call(*self.ingest_args(str(live)))
        assert failed.returncode == 2
        assert "staging" in failed.stderr
        assert not live.exists()
        other = self.live_db().parent / "staging" / "records-other.sqlite3"
        failed = self.failed_call(*self.ingest_args(str(other)))
        assert failed.returncode == 2
        assert "staging" in failed.stderr
        assert not other.exists()
        result = self.parsed(*self.ingest_args(work_db))
        assert result["ingested"] == 1

    def test_ingest_no_cache_override_scopes_to_temp_prefix(self) -> None:
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "nc", "--no-cache")
        work_db = Path(prepared["work_db"])
        assert work_db.parent.parent == Path("/tmp")
        assert work_db.parent.name.startswith("qwen-github-records-")
        args = self.ingest_args(str(work_db), run_id="nc")
        self.parsed(*args, "--no-cache")
        failed = self.failed_call(*args)
        assert failed.returncode == 2
        assert "staging" in failed.stderr
        failed = self.failed_call(
            *args,
            "--db",
            str(work_db.parent / "other-v1.sqlite3"),
            "--no-cache",
        )
        assert failed.returncode == 2
        assert "no-cache" in failed.stderr
        self.call("abort", *self.common(), "--db", str(work_db))
        assert not work_db.exists()

    def test_abort_rejects_database_outside_project_staging(self) -> None:
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        work_db = Path(prepared["work_db"])
        foreign = (
            Path(self.temp.name) / "other-project" / "github" / "staging" / "records-v1.sqlite3"
        )
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"foreign")
        failed = self.failed_call("abort", *self.common(), "--db", str(foreign))
        assert failed.returncode == 2
        assert "refusing to remove" in failed.stderr
        assert foreign.exists()
        self.call("abort", *self.common(), "--db", str(work_db))
        assert not work_db.exists()

    def test_abort_rejects_unrelated_file_with_no_cache_prefix(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qwen-github-unrelated-", dir="/tmp") as directory:
            foreign = Path(directory) / "foreign.txt"
            foreign.write_bytes(b"foreign")
            failed = self.failed_call("abort", *self.common(), "--db", str(foreign))
            assert failed.returncode == 2
            assert "refusing to remove" in failed.stderr
            assert foreign.exists()

    def test_ingest_rejects_input_over_byte_cap(self) -> None:
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        work_db = Path(prepared["work_db"])
        failed = self.failed_call(
            *self.ingest_args(
                str(work_db), records=[{"number": 1, "title": "x" * (11 * 1024 * 1024)}]
            )
        )
        assert failed.returncode == 2
        assert "exceeds" in failed.stderr
        queried = self.parsed("query-records", *self.common(), "--db", str(work_db))
        assert queried["records"] == []

    def test_ingest_accepts_input_at_byte_cap(self) -> None:
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        record = {"number": 1, "state": "open", "title": "x" * 64}
        rendered = json.dumps({"records": [record]})
        record["title"] = "x" * (64 + RECORDS_INPUT_BYTES - len(rendered.encode()))
        payload = Path(self.temp.name) / "records.json"
        payload.write_text(json.dumps({"records": [record]}))
        assert payload.stat().st_size == RECORDS_INPUT_BYTES
        result = self.parsed(*self.ingest_args(prepared["work_db"], records=[record]))
        assert result["ingested"] == 1

    def test_project_directory_is_required(self) -> None:
        failed = self.call("status", "--repo", REPO, check=False)
        assert failed.returncode == 2
        assert "QWEN_CODE_PROJECT_DIR" in failed.stderr

    def test_prepare_for_other_repository_leaves_committed_cache_intact(self) -> None:
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        payload = Path(self.temp.name) / "records.json"
        payload.write_text(
            json.dumps({"records": [{"number": 1, "state": "open", "title": "Committed record"}]})
        )
        self.parsed(
            "ingest-records",
            *self.common(),
            "--run-id",
            "first",
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
        )
        live_a = Path(committed["committed"])
        assert live_a == self.live_db()
        with sqlite3.connect(live_a) as connection:
            before = (
                connection.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[
                    0
                ],
                connection.execute("SELECT COUNT(*) FROM records").fetchone()[0],
            )
        other = "example/other-repo"
        prepared_b = self.parsed(
            "prepare-records",
            "--repo",
            other,
            "--project-dir",
            str(self.project_dir),
            "--run-id",
            "second",
        )
        assert live_a.is_file()
        with sqlite3.connect(live_a) as connection:
            after = (
                connection.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[
                    0
                ],
                connection.execute("SELECT COUNT(*) FROM records").fetchone()[0],
            )
        assert before == after
        assert not list(self.project_dir.glob("github/**/*.invalid-*"))
        assert Path(prepared_b["live_db"]) == self.live_db(other)
        assert Path(prepared_b["work_db"]).parent == self.live_db(other).parent / "staging"

    def test_prepare_with_foreign_cache_in_place_fails_cleanly(self) -> None:
        other = "example/other-repo"
        other_common = ["--repo", other, "--project-dir", str(self.project_dir)]
        prepared = self.parsed("prepare-records", *other_common, "--run-id", "other")
        committed = self.parsed(
            "commit-records",
            *other_common,
            "--run-id",
            "other",
            "--db",
            prepared["work_db"],
            "--base-generation",
            "0",
            "--synced-at",
            "2026-08-30T00:00:00Z",
            "--default-sha",
            "abc",
        )
        live_a = self.live_db()
        live_a.parent.mkdir(parents=True)
        shutil.move(committed["committed"], str(live_a))
        failed = self.call("prepare-records", *self.common(), "--run-id", "next", check=False)
        assert failed.returncode == 2
        assert "identity" in failed.stderr
        assert live_a.is_file()
        assert not list(self.project_dir.glob("github/**/*.invalid-*"))

    def test_linked_records_beyond_limit_are_deterministic_across_processes(self) -> None:
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        issues = Path(self.temp.name) / "issues.json"
        issues.write_text(
            json.dumps(
                {
                    "records": [
                        {"number": n, "state": "open", "title": f"Issue {n}"} for n in range(1, 16)
                    ]
                }
            )
        )
        pulls = Path(self.temp.name) / "pulls.json"
        pulls.write_text(
            json.dumps(
                {
                    "records": [
                        {"number": n, "state": "open", "title": f"Pull {n}"} for n in range(1, 16)
                    ]
                }
            )
        )
        self.parsed(
            "ingest-records",
            *self.common(),
            "--run-id",
            "first",
            "--db",
            prepared["work_db"],
            "--kind",
            "issue",
            "--input",
            str(issues),
            "--source",
            "test",
        )
        self.parsed(
            "ingest-records",
            *self.common(),
            "--run-id",
            "first",
            "--db",
            prepared["work_db"],
            "--kind",
            "pull",
            "--input",
            str(pulls),
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
        )
        live = Path(committed["committed"])
        linked_file = Path(self.temp.name) / "linked.json"
        linked_file.write_text(
            json.dumps(
                [{"kind": "pull", "number": n} for n in range(15, 0, -1)]
                + [{"kind": "issue", "number": n} for n in range(15, 0, -1)]
            )
        )
        outputs = []
        for seed in ("0", "1"):
            result = self.call(
                "query-records",
                *self.common(),
                "--db",
                str(live),
                "--linked",
                str(linked_file),
                "--limit",
                "25",
                env={"PYTHONHASHSEED": seed},
            )
            outputs.append(json.loads(result.stdout))
        assert outputs[0] == outputs[1]
        records = outputs[0]["records"]
        assert [(record["kind"], record["number"]) for record in records] == (
            [("issue", n) for n in range(1, 16)] + [("pull", n) for n in range(1, 11)]
        )
        assert outputs[0]["has_more"] is True
        assert outputs[0]["linked_dropped"] == 5
