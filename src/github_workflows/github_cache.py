#!/usr/bin/env python3
"""Private transactional GitHub-record history shared by Qwen workflows.

The calling supervisor owns all network access and interpretation. This helper stores normalized
GitHub records and provides deterministic text search.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def _project_root_from_argv(argv: list[str]) -> Path | None:
    """Return the explicitly supplied project root, if any."""
    try:
        index = argv.index("--project-root")
    except ValueError:
        return None
    if index + 1 >= len(argv):
        return None
    return Path(argv[index + 1]).expanduser().resolve()


def _bootstrap_project_python() -> None:
    """Prefer a project's SQLite-capable virtualenv before importing sqlite3."""
    project_root = _project_root_from_argv(sys.argv[1:])
    if project_root is None:
        os.environ.setdefault("GITHUB_CACHE_RUNTIME_SOURCE", "shebang-python")
        return
    candidate = project_root / ".venv" / "bin" / "python"
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        os.environ["GITHUB_CACHE_RUNTIME_FALLBACK"] = "project .venv Python is unavailable"
        os.environ.setdefault("GITHUB_CACHE_RUNTIME_SOURCE", "shebang-python")
        return
    current = Path(sys.executable).resolve()
    resolved_candidate = candidate.resolve()
    if current == resolved_candidate:
        os.environ["GITHUB_CACHE_RUNTIME_SOURCE"] = "project-venv"
        return
    try:
        probe = subprocess.run(
            [str(candidate), "-c", "import sqlite3"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        probe = None
    if probe is None or probe.returncode != 0:
        os.environ["GITHUB_CACHE_RUNTIME_FALLBACK"] = "project .venv Python lacks sqlite3"
        os.environ.setdefault("GITHUB_CACHE_RUNTIME_SOURCE", "shebang-python")
        return
    environment = os.environ.copy()
    environment["GITHUB_CACHE_RUNTIME_SOURCE"] = "project-venv"
    os.execve(
        str(candidate), [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]], environment
    )


_bootstrap_project_python()

try:
    import sqlite3
except ImportError as error:  # pragma: no cover - depends on the host Python build
    raise SystemExit(
        "github-cache: neither the selected project Python nor the fallback Python provides sqlite3"
    ) from error


RECORDS_SCHEMA_VERSION = 1
RECORDS_DB = "records-v1.sqlite3"
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def normalized_repo(value: str) -> str:
    parts = [part for part in value.strip().split("/") if part]
    if len(parts) != 2 or any(part in {".", ".."} for part in parts):
        raise ValueError("repository must be OWNER/REPO")
    return "/".join(parts)


def validated_run_id(value: str) -> str:
    if not RUN_ID_RE.fullmatch(value):
        raise ValueError("run-id must contain only letters, numbers, dot, underscore, and hyphen")
    return value


def repo_dir(project_dir: Path, repo: str) -> Path:
    normalized_repo(repo)
    return project_dir.expanduser().resolve() / "github"


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def secure_file(path: Path) -> None:
    if path.exists():
        os.chmod(path, 0o600)


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"cache database does not exist: {path}")
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM metadata")
    }


def initialize_common(
    connection: sqlite3.Connection, repo: str, kind: str, schema_version: int
) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    values = {
        "schema_version": str(schema_version),
        "database_kind": kind,
        "repository": normalized_repo(repo),
        "generation": "0",
        "last_sync_at": "",
    }
    values["default_sha"] = ""
    connection.executemany(
        "INSERT OR IGNORE INTO metadata(key, value) VALUES (?, ?)", values.items()
    )


def initialize_records(connection: sqlite3.Connection, repo: str) -> None:
    initialize_common(connection, repo, "records", RECORDS_SCHEMA_VERSION)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS records (
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
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
            kind UNINDEXED,
            number UNINDEXED,
            title,
            body,
            labels
        );
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) VALUES ('full_history_complete', 'false')"
    )
    connection.commit()


def validate(connection: sqlite3.Connection, repo: str, kind: str) -> dict[str, Any]:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("SQLite integrity check failed")
    meta = metadata(connection)
    if int(meta.get("schema_version", "-1")) != RECORDS_SCHEMA_VERSION:
        raise ValueError("cache schema version is incompatible")
    if meta.get("repository") != normalized_repo(repo):
        raise ValueError("cache repository identity does not match")
    if meta.get("database_kind") != "records":
        raise ValueError("cache database kind does not match")
    count = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    return {"metadata": meta, "count": count}


def live_path(directory: Path, kind: str) -> Path:
    return directory / RECORDS_DB


def prepare_database(args: argparse.Namespace, kind: str) -> None:
    run_id = validated_run_id(args.run_id)
    directory = repo_dir(args.cache_root, args.repo)
    secure_directory(directory)
    live = live_path(directory, kind)
    if args.no_cache:
        temporary = Path(tempfile.mkdtemp(prefix=f"qwen-github-{kind}-", dir="/tmp"))
        os.chmod(temporary, 0o700)
        work = temporary / live.name
        with connect(work) as connection:
            initialize_records(connection, args.repo)
        secure_file(work)
        print(
            json.dumps({"persistent": False, "work_db": str(work), "base_generation": 0}, indent=2)
        )
        return
    staging = directory / "staging"
    secure_directory(staging)
    work = staging / f"{kind}-{run_id}.sqlite3"
    if work.exists():
        raise RuntimeError(f"staging database already exists: {work}")
    base_generation = 0
    mode = "new"
    reuse_live = live.exists() and not args.rebuild
    if reuse_live:
        try:
            with connect(live) as current:
                previous = validate(current, args.repo, kind)
                base_generation = int(previous["metadata"]["generation"])
        except (ValueError, sqlite3.DatabaseError):
            suffix = utc_now().strftime("%Y%m%dT%H%M%SZ")
            backup = directory / f"{live.name}.invalid-{suffix}"
            if backup.exists():
                raise RuntimeError(f"cache recovery backup already exists: {backup}")
            os.replace(live, backup)
            secure_file(backup)
            reuse_live = False
            mode = "recovery"
        else:
            shutil.copy2(live, work)
            with connect(work) as connection, connection:
                compact_existing_records(connection)
            mode = "reuse"
    if not reuse_live:
        with connect(work) as connection:
            initialize_records(connection, args.repo)
    secure_file(work)
    print(
        json.dumps(
            {
                "persistent": True,
                "mode": "rebuild" if args.rebuild else mode,
                "live_db": str(live),
                "work_db": str(work),
                "base_generation": base_generation,
            },
            indent=2,
        )
    )


def first(record: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return None


def names(value: Any) -> list[str]:
    output: list[str] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            output.append(item)
        elif isinstance(item, dict):
            candidate = first(item, "name", "login")
            if isinstance(candidate, str):
                output.append(candidate)
    return output


def normalize_record(
    record: dict[str, Any], kind: str, source: str, fetched_at: str
) -> dict[str, Any]:
    number = first(record, "number", "issue_number", "pull_number")
    if not isinstance(number, int):
        raise ValueError("record number must be an integer")
    labels = names(first(record, "labels") or [])
    assignees = names(first(record, "assignees") or [])
    base = first(record, "base_ref", "base")
    head = first(record, "head_ref", "head")
    if isinstance(base, dict):
        base = first(base, "ref", "name")
    if isinstance(head, dict):
        head = first(head, "ref", "name")
    title = str(first(record, "title") or "")
    state = str(first(record, "state") or "unknown").lower()
    created_at = first(record, "created_at", "createdAt")
    updated_at = first(record, "updated_at", "updatedAt")
    closed_at = first(record, "closed_at", "closedAt")
    merged_at = first(record, "merged_at", "mergedAt")
    url = first(record, "url", "html_url")
    head_sha = first(record, "head_sha", "headSha")
    canonical = json.dumps(
        {
            "state": state,
            "title": title,
            "labels": labels,
            "assignees": assignees,
            "created_at": created_at,
            "updated_at": updated_at,
            "closed_at": closed_at,
            "merged_at": merged_at,
            "url": url,
            "base_ref": base,
            "head_ref": head,
            "head_sha": head_sha,
        },
        sort_keys=True,
    )
    result = {
        "kind": kind,
        "number": number,
        "state": state,
        "state_reason": first(record, "state_reason", "stateReason"),
        "title": title,
        "body": "",
        "comments_json": "[]",
        "labels_json": json.dumps(labels, sort_keys=True),
        "assignees_json": json.dumps(assignees, sort_keys=True),
        "relationships_json": "{}",
        "commits_json": "[]",
        "created_at": created_at,
        "updated_at": updated_at,
        "closed_at": closed_at,
        "merged_at": merged_at,
        "url": url,
        "base_ref": base,
        "head_ref": head,
        "head_sha": head_sha,
        "content_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "hydration": "summary",
        "source": source,
        "fetched_at": fetched_at,
    }
    return result


def record_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and isinstance(payload.get("records"), list):
        records = payload["records"]
    elif isinstance(payload, dict):
        records = [payload]
    else:
        raise ValueError("input must contain one record or a record list")
    if not all(isinstance(item, dict) for item in records):
        raise ValueError("every record must be an object")
    return records


def upsert_record(connection: sqlite3.Connection, item: dict[str, Any]) -> None:
    columns = list(item)
    assignments = ", ".join(
        f"{column}=excluded.{column}" for column in columns if column not in {"kind", "number"}
    )
    connection.execute(
        f"INSERT INTO records ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "  # noqa: S608 - columns come from normalized records
        f"ON CONFLICT(kind, number) DO UPDATE SET {assignments}",
        [item[column] for column in columns],
    )
    connection.execute(
        "DELETE FROM records_fts WHERE kind=? AND number=?", (item["kind"], item["number"])
    )
    connection.execute(
        "INSERT INTO records_fts(kind, number, title, body, labels) VALUES (?, ?, ?, ?, ?)",
        (item["kind"], item["number"], item["title"], item["body"], item["labels_json"]),
    )


def compact_existing_records(connection: sqlite3.Connection) -> None:
    """Remove detail payloads from every record copied from a legacy live cache."""
    records = list(connection.execute("SELECT * FROM records"))
    for row in records:
        raw = dict(row)
        raw["labels"] = json.loads(raw.pop("labels_json") or "[]")
        raw["assignees"] = json.loads(raw.pop("assignees_json") or "[]")
        item = normalize_record(
            raw,
            raw["kind"],
            raw.get("source") or "cache-migration",
            raw.get("fetched_at") or iso_utc(utc_now()),
        )
        upsert_record(connection, item)


def ingest_records(args: argparse.Namespace) -> None:
    payload = json.loads(args.input.read_text())
    fetched_at = args.fetched_at or iso_utc(utc_now())
    items = [
        normalize_record(item, args.kind, args.source, fetched_at) for item in record_list(payload)
    ]
    with connect(args.db) as connection:
        validate(connection, args.repo, "records")
        with connection:
            for item in items:
                upsert_record(connection, item)
    secure_file(args.db)
    print(json.dumps({"ingested": len(items), "kind": args.kind}))


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    labels = json.loads(row["labels_json"])
    title = row["title"]
    summary = title if not labels else f"{title} [labels: {', '.join(sorted(labels))}]"
    result = {
        "kind": row["kind"],
        "number": row["number"],
        "url": row["url"],
        "title": title,
        "summary": summary,
        "state": row["state"],
        "state_reason": row["state_reason"],
        "labels": labels,
        "assignees": json.loads(row["assignees_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "closed_at": row["closed_at"],
        "merged_at": row["merged_at"],
        "base_ref": row["base_ref"],
        "head_ref": row["head_ref"],
        "head_sha": row["head_sha"],
    }
    return {key: value for key, value in result.items() if value is not None}


def linked_keys(path: Path | None) -> set[tuple[str, int]]:
    if path is None:
        return set()
    payload = json.loads(path.read_text())
    return {
        (item["kind"], item["number"])
        for item in payload
        if isinstance(item, dict)
        and item.get("kind") in {"issue", "pull"}
        and isinstance(item.get("number"), int)
    }


def event_time(row: sqlite3.Row) -> dt.datetime | None:
    if row["kind"] == "pull" and row["merged_at"]:
        return parse_time(row["merged_at"])
    return parse_time(row["closed_at"]) or parse_time(row["updated_at"])


def eligible(row: sqlite3.Row, cutoff: dt.datetime | None, linked: set[tuple[str, int]]) -> bool:
    if cutoff is None or row["state"] == "open" or (row["kind"], row["number"]) in linked:
        return True
    timestamp = event_time(row)
    return timestamp is not None and timestamp >= cutoff


def safe_match_query(text: str) -> str | None:
    tokens: list[str] = []
    for token in TOKEN_RE.findall(text.lower()):
        if token not in tokens:
            tokens.append(token)
    return " OR ".join(f'"{token}"' for token in tokens[:32]) or None


def query_records(args: argparse.Namespace) -> None:
    cutoff = parse_time(args.cutoff) if args.cutoff else None
    if args.cutoff and cutoff is None:
        raise ValueError("cutoff must be an ISO-8601 timestamp")
    linked = linked_keys(args.linked)
    text = args.terms or (args.terms_file.read_text() if args.terms_file else "")
    match = safe_match_query(text)
    with connect_readonly(args.db) as connection:
        validate(connection, args.repo, "records")
        rows: Iterable[sqlite3.Row]
        if match:
            rows = connection.execute(
                "SELECT r.* FROM records_fts f JOIN records r ON r.kind=f.kind AND r.number=f.number "
                "WHERE records_fts MATCH ? ORDER BY bm25(records_fts)",
                (f"{{title labels}} : ({match})",),
            )
        else:
            rows = connection.execute("SELECT * FROM records ORDER BY kind, number")
        selected: dict[tuple[str, int], sqlite3.Row] = {}
        for kind, number in linked:
            row = connection.execute(
                "SELECT * FROM records WHERE kind=? AND number=?", (kind, number)
            ).fetchone()
            if row:
                selected[(kind, number)] = row
        for row in rows:
            if args.kind and row["kind"] != args.kind:
                continue
            if args.state and row["state"] != args.state:
                continue
            if eligible(row, cutoff, linked):
                selected[(row["kind"], row["number"])] = row
            if args.limit and len(selected) > args.limit:
                break
        selected_records = list(selected.values())
        has_more = bool(args.limit and len(selected_records) > args.limit)
        if args.limit:
            selected_records = selected_records[: args.limit]
        result = {
            "cutoff": iso_utc(cutoff) if cutoff else None,
            "has_more": has_more,
            "records": [row_dict(row) for row in selected_records],
        }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
        os.chmod(args.output, 0o600)
    else:
        print(rendered, end="")


def runtime_info(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            {
                "python_executable": sys.executable,
                "runtime_source": os.environ.get("GITHUB_CACHE_RUNTIME_SOURCE", "unknown"),
                "fallback_reason": os.environ.get("GITHUB_CACHE_RUNTIME_FALLBACK"),
                "sqlite_version": sqlite3.sqlite_version,
                "project_root": str(args.project_root.resolve()) if args.project_root else None,
            },
            sort_keys=True,
        )
    )


def commit_database(args: argparse.Namespace, kind: str) -> None:
    run_id = validated_run_id(args.run_id)
    directory = repo_dir(args.cache_root, args.repo)
    staging = (directory / "staging").resolve()
    work = args.db.resolve()
    if work.parent != staging or work.name != f"{kind}-{run_id}.sqlite3":
        raise ValueError("staging database path does not match this transaction")
    lock = directory / "commit.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        live = live_path(directory, kind)
        live_generation = 0
        if live.exists():
            with connect(live) as current:
                live_generation = int(validate(current, args.repo, kind)["metadata"]["generation"])
        if live_generation != args.base_generation:
            raise RuntimeError(
                f"cache generation conflict: expected {args.base_generation}, found {live_generation}"
            )
        with connect(work) as connection:
            validate(connection, args.repo, kind)
            removed = 0
            updates = {
                "generation": str(live_generation + 1),
                "last_sync_at": args.synced_at,
            }
            updates["default_sha"] = args.default_sha
            updates["full_history_complete"] = "true" if args.full_history_complete else "false"
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                updates.items(),
            )
            connection.commit()
            validate(connection, args.repo, kind)
        os.replace(work, live)
        secure_file(live)
    finally:
        os.close(descriptor)
    print(
        json.dumps({"committed": str(live), "generation": live_generation + 1, "pruned": removed})
    )


def abort_database(args: argparse.Namespace) -> None:
    path = args.db.resolve()
    if path.parent.name == "staging" or str(path).startswith("/tmp/qwen-github-"):
        path.unlink(missing_ok=True)
        if str(path.parent).startswith("/tmp/qwen-github-"):
            path.parent.rmdir()
    else:
        raise ValueError("refusing to remove a non-staging database")
    print(json.dumps({"aborted": str(path)}))


def status(args: argparse.Namespace) -> None:
    directory = repo_dir(args.cache_root, args.repo)
    result: dict[str, Any] = {"cache_dir": str(directory)}
    path = live_path(directory, "records")
    if path.exists():
        with connect(path) as connection:
            result["records"] = {
                "exists": True,
                **validate(connection, args.repo, "records"),
                "path": str(path),
            }
    else:
        result["records"] = {"exists": False, "path": str(path)}
    print(json.dumps(result, indent=2, sort_keys=True))


def add_common_repo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True)
    parser.add_argument("--project-dir", type=Path)


def add_prepare(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], name: str, kind: str
) -> None:
    command = sub.add_parser(name)
    add_common_repo(command)
    command.add_argument("--run-id", required=True)
    cache_mode = command.add_mutually_exclusive_group()
    cache_mode.add_argument("--rebuild", action="store_true")
    cache_mode.add_argument("--no-cache", action="store_true")
    command.set_defaults(handler=lambda args: prepare_database(args, kind))


def add_commit(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], name: str, kind: str
) -> None:
    command = sub.add_parser(name)
    add_common_repo(command)
    command.add_argument("--run-id", required=True)
    command.add_argument("--db", type=Path, required=True)
    command.add_argument("--base-generation", type=int, required=True)
    command.add_argument("--synced-at", required=True)
    command.add_argument("--default-sha", required=True)
    command.add_argument("--full-history-complete", action="store_true")
    command.set_defaults(handler=lambda args: commit_database(args, kind))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        help="prefer this project's SQLite-capable .venv Python when available",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    add_prepare(sub, "prepare-records", "records")

    command = sub.add_parser("ingest-records")
    add_common_repo(command)
    command.add_argument("--db", type=Path, required=True)
    command.add_argument("--kind", choices=("issue", "pull"), required=True)
    command.add_argument("--input", type=Path, required=True)
    command.add_argument("--source", required=True)
    command.add_argument("--fetched-at")
    command.set_defaults(handler=ingest_records)

    command = sub.add_parser("query-records")
    add_common_repo(command)
    command.add_argument("--db", type=Path, required=True)
    command.add_argument("--cutoff")
    command.add_argument("--linked", type=Path)
    command.add_argument("--terms")
    command.add_argument("--terms-file", type=Path)
    command.add_argument("--kind", choices=("issue", "pull"))
    command.add_argument("--state", choices=("open", "closed"))
    command.add_argument("--limit", type=int, default=0)
    command.add_argument("--output", type=Path)
    command.set_defaults(handler=query_records)

    add_commit(sub, "commit-records", "records")

    command = sub.add_parser("abort")
    command.add_argument("--db", type=Path, required=True)
    command.set_defaults(handler=abort_database)

    command = sub.add_parser("status")
    add_common_repo(command)
    command.set_defaults(handler=status)

    command = sub.add_parser("runtime-info")
    command.set_defaults(handler=runtime_info)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    configured = getattr(args, "project_dir", None) or os.environ.get("QWEN_CODE_PROJECT_DIR")
    if args.command not in {"abort", "runtime-info"} and not configured:
        raise ValueError(
            "QWEN_CODE_PROJECT_DIR is required outside tests; use --project-dir explicitly"
        )
    args.cache_root = Path(configured).expanduser() if configured else Path(".")
    args.handler(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        ValueError,
        RuntimeError,
        sqlite3.DatabaseError,
        OSError,
        json.JSONDecodeError,
    ) as error:
        print(f"github-cache: {error}", file=os.sys.stderr)
        raise SystemExit(2)
