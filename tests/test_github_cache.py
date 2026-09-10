from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import github_workflows.github_cache as github_cache_module
from github_workflows.github_cache import RECORDS_INPUT_BYTES, RECORDS_SCHEMA_VERSION
from github_workflows.models import HistoryQueryRequest

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
        assert "pruned" not in committed
        with sqlite3.connect(expected) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(records)")}
        for column in ("body", "comments_json", "relationships_json", "commits_json", "hydration"):
            assert column not in columns

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

    def commit_args(self, work_db: str, run_id: str, base_generation: int | str) -> list[str]:
        return [
            "commit-records",
            *self.common(),
            "--run-id",
            run_id,
            "--db",
            work_db,
            "--base-generation",
            str(base_generation),
            "--synced-at",
            "2026-08-30T00:00:00Z",
            "--default-sha",
            "abc",
        ]

    def seed_legacy_live(self, records: list[dict[str, Any]], generation: int = 3) -> Path:
        """Seed a pre-migration (schema v1) committed records cache with detail payloads."""
        live = self.live_db()
        live.parent.mkdir(parents=True)
        with sqlite3.connect(live) as connection:
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                [
                    ("schema_version", "1"),
                    ("database_kind", "records"),
                    ("repository", REPO),
                    ("generation", str(generation)),
                    ("last_sync_at", ""),
                    ("default_sha", ""),
                    ("full_history_complete", "true"),
                ],
            )
            connection.execute(
                """
                CREATE TABLE records (
                    kind TEXT NOT NULL CHECK(kind IN ('issue', 'pull')),
                    number INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    state_reason TEXT,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    comments_json TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    assignees_json TEXT NOT NULL,
                    relationships_json TEXT NOT NULL,
                    commits_json TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT,
                    closed_at TEXT,
                    merged_at TEXT,
                    url TEXT,
                    base_ref TEXT,
                    head_ref TEXT,
                    head_sha TEXT,
                    content_sha256 TEXT NOT NULL,
                    hydration TEXT NOT NULL CHECK(hydration IN ('summary', 'detail')),
                    source TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY(kind, number)
                )
                """
            )
            connection.execute(
                """
                CREATE VIRTUAL TABLE records_fts USING fts5(
                    kind UNINDEXED,
                    number UNINDEXED,
                    title,
                    body,
                    labels
                )
                """
            )
            for record in records:
                connection.execute(
                    """INSERT INTO records (
                        kind, number, state, title, body, comments_json, labels_json,
                        assignees_json, relationships_json, commits_json, content_sha256,
                        hydration, source, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record["kind"],
                        record["number"],
                        record["state"],
                        record["title"],
                        record.get("body", ""),
                        record.get("comments_json", "[]"),
                        record.get("labels_json", "[]"),
                        record.get("assignees_json", "[]"),
                        record.get("relationships_json", "{}"),
                        record.get("commits_json", "[]"),
                        record.get("content_sha256", "legacy"),
                        record.get("hydration", "summary"),
                        record.get("source", "legacy"),
                        record.get("fetched_at", "2024-01-01T00:00:00Z"),
                    ),
                )
                connection.execute(
                    "INSERT INTO records_fts(kind, number, title, body, labels) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        record["kind"],
                        record["number"],
                        record["title"],
                        record.get("body", ""),
                        record.get("labels_json", "[]"),
                    ),
                )
        return live

    def test_prepare_reuses_current_schema_cache_without_full_table_rewrite(self) -> None:
        first = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        self.parsed(*self.ingest_args(first["work_db"]))
        self.parsed(*self.commit_args(first["work_db"], "first", "0"))
        live = self.live_db()
        reused = self.parsed("prepare-records", *self.common(), "--run-id", "second")
        assert reused["mode"] == "reuse"
        assert reused["base_generation"] == 1
        work = Path(reused["work_db"])
        # No per-prepare legacy rewrite remains: the work copy is an untouched
        # copy of the committed cache.
        assert work.read_bytes() == live.read_bytes()
        queried = self.parsed(
            "query-records", *self.common(), "--db", str(work), "--terms", "Scoped"
        )
        assert [item["number"] for item in queried["records"]] == [1]

    def test_legacy_cache_migrates_exactly_once(self) -> None:
        live = self.seed_legacy_live(
            [
                {
                    "kind": "issue",
                    "number": 7,
                    "state": "closed",
                    "title": "Legacy parser",
                    "body": "secret detail payload",
                    "comments_json": '[{"body": "secret"}]',
                    "labels_json": '["old"]',
                    "relationships_json": '{"pull": 1}',
                    "commits_json": '[{"sha": "abc"}]',
                    "hydration": "detail",
                },
                {
                    "kind": "issue",
                    "number": 8,
                    "state": "open",
                    "title": "Legacy search",
                    "labels_json": '["bug"]',
                },
            ],
            generation=3,
        )
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        assert prepared["mode"] == "reuse"
        assert prepared["base_generation"] == 3
        work = Path(prepared["work_db"])
        with sqlite3.connect(work) as connection:
            meta = {row[0]: row[1] for row in connection.execute("SELECT key, value FROM metadata")}
            columns = {row[1] for row in connection.execute("PRAGMA table_info(records)")}
            fts_columns = {row[1] for row in connection.execute("PRAGMA table_info(records_fts)")}
            row = connection.execute(
                "SELECT title, labels_json, source FROM records WHERE number=7"
            ).fetchone()
        for column in ("body", "comments_json", "relationships_json", "commits_json", "hydration"):
            assert column not in columns
            assert column not in fts_columns
        assert meta["schema_version"] == str(RECORDS_SCHEMA_VERSION)
        assert meta["generation"] == "3"
        assert row == ("Legacy parser", '["old"]', "legacy")
        stripped = self.parsed(
            "query-records", *self.common(), "--db", str(work), "--terms", "secret"
        )
        assert stripped["records"] == []
        self.parsed(*self.commit_args(str(work), "first", prepared["base_generation"]))
        with sqlite3.connect(live) as connection:
            meta = {row[0]: row[1] for row in connection.execute("SELECT key, value FROM metadata")}
        assert meta["schema_version"] == str(RECORDS_SCHEMA_VERSION)
        assert meta["generation"] == "4"
        # Query output contract unchanged: logical record fields survive migration.
        queried = self.parsed(
            "query-records", *self.common(), "--db", str(live), "--terms", "Legacy"
        )
        by_number = {record["number"]: record for record in queried["records"]}
        assert set(by_number) == {7, 8}
        assert by_number[7]["title"] == "Legacy parser"
        assert by_number[7]["labels"] == ["old"]
        assert by_number[7]["state"] == "closed"
        assert by_number[8]["title"] == "Legacy search"
        assert by_number[8]["labels"] == ["bug"]
        assert by_number[8]["state"] == "open"
        # The next prepare sees the current schema marker and performs no rewrite.
        reused = self.parsed("prepare-records", *self.common(), "--run-id", "second")
        assert reused["mode"] == "reuse"
        assert reused["base_generation"] == 4
        assert Path(reused["work_db"]).read_bytes() == live.read_bytes()

    def test_legacy_migration_failure_recovers_cache_instead_of_committing_partial(self) -> None:
        live = self.seed_legacy_live(
            [{"kind": "issue", "number": 0, "state": "open", "title": "Bad number"}],
            generation=2,
        )
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        assert prepared["mode"] == "recovery"
        assert prepared["base_generation"] == 0
        assert not live.exists()
        assert len(list(self.project_dir.glob("github/**/*.invalid-*"))) == 1
        queried = self.parsed(
            "query-records", *self.common(), "--db", prepared["work_db"], "--terms", "Bad"
        )
        assert queried["records"] == []

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

    def test_prepare_resumes_valid_staging_and_recovers_empty_staging(self) -> None:
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        self.parsed(*self.ingest_args(prepared["work_db"]))
        resumed = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        assert resumed["mode"] == "resume"
        queried = self.parsed(
            "query-records", *self.common(), "--db", resumed["work_db"], "--terms", "Scoped"
        )
        assert [item["number"] for item in queried["records"]] == [1]

        Path(resumed["work_db"]).write_bytes(b"")
        recovered = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        assert recovered["mode"] == "recovered-empty"
        assert Path(recovered["work_db"]).stat().st_size > 0

    def test_rebuild_replaces_valid_staging_instead_of_resuming_it(self) -> None:
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        self.parsed(*self.ingest_args(prepared["work_db"]))

        rebuilt = self.parsed("prepare-records", *self.common(), "--run-id", "first", "--rebuild")

        assert rebuilt["mode"] == "rebuild"
        assert rebuilt["base_generation"] == 0
        queried = self.parsed(
            "query-records", *self.common(), "--db", rebuilt["work_db"], "--terms", "Scoped"
        )
        assert queried["records"] == []

    def test_rebuild_commits_against_precommitted_live_cache(self) -> None:
        first = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        self.parsed(*self.ingest_args(first["work_db"]))
        committed = self.parsed(*self.commit_args(first["work_db"], "first", "0"))
        assert committed["generation"] == 1
        rebuilt = self.parsed("prepare-records", *self.common(), "--run-id", "second", "--rebuild")
        assert rebuilt["mode"] == "rebuild"
        assert rebuilt["base_generation"] == 1
        self.parsed(
            *self.ingest_args(
                rebuilt["work_db"],
                "second",
                records=[{"number": 2, "state": "open", "title": "Rebuilt"}],
            )
        )
        committed = self.parsed(*self.commit_args(rebuilt["work_db"], "second", "1"))
        assert committed["generation"] == 2
        live = self.live_db()
        queried = self.parsed(
            "query-records", *self.common(), "--db", str(live), "--terms", "Rebuilt"
        )
        assert [record["number"] for record in queried["records"]] == [2]
        stale = self.parsed("query-records", *self.common(), "--db", str(live), "--terms", "Scoped")
        assert stale["records"] == []

    def test_rebuild_recovers_corrupted_committed_cache_with_backup(self) -> None:
        for corrupted in (b"not sqlite", b""):
            shutil.rmtree(self.project_dir / "github", ignore_errors=True)
            first = self.parsed("prepare-records", *self.common(), "--run-id", "first")
            self.parsed(*self.ingest_args(first["work_db"]))
            self.parsed(*self.commit_args(first["work_db"], "first", "0"))
            live = self.live_db()
            assert live.is_file()
            live.write_bytes(corrupted)
            rebuilt = self.parsed(
                "prepare-records", *self.common(), "--run-id", "second", "--rebuild"
            )
            assert rebuilt["mode"] == "rebuild"
            assert rebuilt["base_generation"] == 0
            assert not live.exists()
            backups = list(self.project_dir.glob("github/**/*.invalid-*"))
            assert len(backups) == 1
            assert backups[0].read_bytes() == corrupted
            # The rebuild completes against the recovered base generation.
            self.parsed(*self.ingest_args(rebuilt["work_db"], "second"))
            committed = self.parsed(*self.commit_args(rebuilt["work_db"], "second", "0"))
            assert committed["generation"] == 1
            queried = self.parsed(
                "query-records", *self.common(), "--db", committed["committed"], "--terms", "Scoped"
            )
            assert [record["number"] for record in queried["records"]] == [1]

    def test_commit_quarantines_cache_corrupted_after_prepare_and_reports_conflict(self) -> None:
        first = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        self.parsed(*self.ingest_args(first["work_db"]))
        self.parsed(*self.commit_args(first["work_db"], "first", "0"))
        live = self.live_db()
        second = self.parsed("prepare-records", *self.common(), "--run-id", "second")
        assert second["mode"] == "reuse"
        assert second["base_generation"] == 1
        live.write_bytes(b"not sqlite")
        failed = self.failed_call(*self.commit_args(second["work_db"], "second", "1"))
        assert failed.returncode == 2
        assert "generation conflict" in failed.stderr
        assert "Traceback" not in failed.stderr
        assert not live.exists()
        backups = list(self.project_dir.glob("github/**/*.invalid-*"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == b"not sqlite"
        # A fresh base-0 transaction recovers the quarantined state.
        third = self.parsed("prepare-records", *self.common(), "--run-id", "third")
        assert third["base_generation"] == 0
        self.parsed(*self.ingest_args(third["work_db"], "third"))
        committed = self.parsed(*self.commit_args(third["work_db"], "third", "0"))
        assert committed["generation"] == 1
        queried = self.parsed(
            "query-records", *self.common(), "--db", committed["committed"], "--terms", "Scoped"
        )
        assert [record["number"] for record in queried["records"]] == [1]

    def test_invalid_staging_requires_abort_and_abort_does_not_follow_symlink(self) -> None:
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        work_db = Path(prepared["work_db"])
        work_db.write_bytes(b"not sqlite")
        failed = self.failed_call("prepare-records", *self.common(), "--run-id", "first")
        assert failed.returncode == 2
        assert "call abort, then prepare" in failed.stderr
        self.call("abort", *self.common(), "--db", str(work_db))
        assert not work_db.exists()

        target = Path(self.temp.name) / "must-survive"
        target.write_text("important", encoding="utf-8")
        work_db.symlink_to(target)
        refused = self.failed_call("abort", *self.common(), "--db", str(work_db))
        assert refused.returncode == 2
        assert "unsafe" in refused.stderr
        assert work_db.is_symlink()
        assert target.read_text(encoding="utf-8") == "important"

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

    def test_abort_leaves_nfs_staging_artifacts_untouched(self) -> None:
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        artifact = Path(prepared["work_db"]).parent / ".nfs123"
        artifact.write_bytes(b"busy")
        failed = self.failed_call("abort", *self.common(), "--db", str(artifact))
        assert failed.returncode == 2
        assert artifact.read_bytes() == b"busy"

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

    def test_ingest_rejects_non_positive_and_boolean_numbers(self) -> None:
        prepared = self.parsed("prepare-records", *self.common(), "--run-id", "first")
        work_db = prepared["work_db"]
        payload = Path(self.temp.name) / "bad-records.json"
        # normalize_record is the guard shared by the artifact-feed path, which
        # skips the request-model boundary; JSON booleans arrive as int subclasses.
        for number in (0, -3, True):
            payload.write_text(
                json.dumps({"records": [{"number": number, "state": "open", "title": "Bad"}]})
            )
            failed = self.failed_call(
                "ingest-records",
                *self.common(),
                "--run-id",
                "first",
                "--db",
                work_db,
                "--kind",
                "issue",
                "--input",
                str(payload),
                "--source",
                "test",
            )
            assert failed.returncode == 2
            assert "positive integer" in failed.stderr
        queried = self.parsed("query-records", *self.common(), "--db", work_db, "--terms", "Bad")
        assert queried["records"] == []

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


class TestQueryBounds:
    """Model-level and statement-level bounds for history query selectors."""

    def setup_method(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp.name) / "qwen-project"

    def teardown_method(self) -> None:
        self.temp.cleanup()

    def seed_records(self, records: list[tuple[int, str, str]]) -> Path:
        repo_dir = github_cache_module.repo_dir(self.project_dir, REPO)
        github_cache_module.secure_directory(repo_dir)
        live = github_cache_module.live_path(repo_dir, "records")
        with github_cache_module.connect(live) as connection:
            github_cache_module.initialize_records(connection, REPO)
            for number, title, state in records:
                item = github_cache_module.normalize_record(
                    {"number": number, "state": state, "title": title},
                    "issue",
                    "test",
                    "2026-08-30T00:00:00Z",
                )
                github_cache_module.upsert_record(connection, item)
        github_cache_module.secure_file(live)
        return live

    def query(
        self,
        live: Path,
        linked: list[dict[str, Any]] | None = None,
        terms: str | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Run query_records in-process, returning the result and traced statements."""
        linked_path: Path | None = None
        if linked is not None:
            linked_path = Path(self.temp.name) / "linked.json"
            linked_path.write_text(json.dumps(linked))
        output = Path(self.temp.name) / "query-result.json"
        statements: list[str] = []
        original = github_cache_module.connect_readonly

        def counting(path: Path) -> sqlite3.Connection:
            connection = original(path)
            connection.set_trace_callback(statements.append)
            return connection

        args = argparse.Namespace(
            cutoff=None,
            linked=linked_path,
            offset=0,
            fill=False,
            terms=terms,
            terms_file=None,
            db=live,
            repo=REPO,
            kind=None,
            state=None,
            limit=0,
            output=output,
        )
        try:
            github_cache_module.connect_readonly = counting
            github_cache_module.query_records(args)
        finally:
            github_cache_module.connect_readonly = original
        return json.loads(output.read_text()), statements

    @staticmethod
    def linked_selects(statements: list[str]) -> list[str]:
        assert not any("kind=? AND number=?" in statement for statement in statements)
        return [statement for statement in statements if "(kind, number) IN" in statement]

    def test_over_cap_linked_list_is_rejected_at_model_level(self) -> None:
        over_cap = [{"kind": "issue", "number": n} for n in range(1, 102)]
        with pytest.raises(ValidationError) as validation:
            HistoryQueryRequest(linked=over_cap)
        errors = validation.value.errors(include_url=False)
        assert errors[0]["loc"] == ("linked",)
        assert errors[0]["type"] == "too_long"
        at_cap = HistoryQueryRequest(linked=[{"kind": "issue", "number": n} for n in range(1, 101)])
        assert len(at_cap.linked) == 100

    def test_large_linked_list_does_not_execute_one_select_per_key(self) -> None:
        live = self.seed_records([(n, f"Record {n}", "open") for n in range(1, 11)])
        phantom = [{"kind": "issue", "number": n} for n in range(1000, 3000)]
        result, statements = self.query(live, linked=phantom, terms="nomatchtoken")
        selects = self.linked_selects(statements)
        assert selects
        assert len(selects) < len(phantom)
        assert (
            len(selects)
            <= (len(phantom) + github_cache_module.LINKED_QUERY_CHUNK - 1)
            // github_cache_module.LINKED_QUERY_CHUNK
        )
        assert result["records"] == []
        assert result["linked_dropped"] == 0

    def test_large_valid_linked_list_returns_linked_records(self) -> None:
        live = self.seed_records([(n, f"Record {n}", "open") for n in range(1, 11)])
        links = [{"kind": "issue", "number": n} for n in range(1000, 3000)]
        present = [{"kind": "issue", "number": number} for number in (7, 2, 5)]
        result, statements = self.query(live, linked=links + present, terms="nomatchtoken")
        selects = self.linked_selects(statements)
        assert (
            len(selects)
            <= (len(links + present) + github_cache_module.LINKED_QUERY_CHUNK - 1)
            // github_cache_module.LINKED_QUERY_CHUNK
        )
        assert [(record["kind"], record["number"]) for record in result["records"]] == [
            ("issue", 2),
            ("issue", 5),
            ("issue", 7),
        ]
        assert result["linked_dropped"] == 0

    def test_no_token_terms_yield_empty_record_set(self) -> None:
        live = self.seed_records([(n, f"Record {n}", "open") for n in range(1, 4)])
        result, _ = self.query(live, terms="a! b?")
        assert result["records"] == []
        assert result["has_more"] is False

    def test_no_token_terms_preserve_linked_record_selection(self) -> None:
        live = self.seed_records([(n, f"Record {n}", "open") for n in range(1, 4)])
        result, _ = self.query(live, terms="a! b?", linked=[{"kind": "issue", "number": 2}])
        assert [(record["kind"], record["number"]) for record in result["records"]] == [
            ("issue", 2)
        ]

    def test_absent_terms_still_yield_unfiltered_listing(self) -> None:
        live = self.seed_records([(n, f"Record {n}", "open") for n in range(1, 4)])
        result, _ = self.query(live)
        assert [record["number"] for record in result["records"]] == [1, 2, 3]

    def test_query_records_rejects_invalid_committed_cache_with_classified_state(self) -> None:
        live = self.seed_records([(1, "Record one", "open")])
        for payload, state in ((b"not a sqlite database", "invalid"), (b"", "empty")):
            live.write_bytes(payload)
            # database_state classifies the identical file state (the contrast
            # against which query_records must fail cleanly, not raw).
            assert (
                github_cache_module.database_state(live, REPO, "records")["staging_state"] == state
            )
            with pytest.raises(ValueError, match=state):
                self.query(live, terms="Record")
