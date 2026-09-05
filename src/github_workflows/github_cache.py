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
AUDIT_SCHEMA_VERSION = 1
RECORDS_DB = "records-v1.sqlite3"
AUDIT_DB = "audit-history-v1.sqlite3"
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")
OBSERVATION_INPUT_BYTES = 10 * 1024 * 1024
OBSERVATION_PHASES = {"structure", "discover", "verify"}
AREA_RE = re.compile(r"area/[a-z0-9][a-z0-9._-]{0,127}")


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
    owner, name = normalized_repo(repo).split("/")
    return project_dir.expanduser().resolve() / "github" / owner / name


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
    values["snapshot_sha" if kind == "audit-history" else "default_sha"] = ""
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


def initialize_audit(connection: sqlite3.Connection, repo: str) -> None:
    initialize_common(connection, repo, "audit-history", AUDIT_SCHEMA_VERSION)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS observations (
            run_id TEXT NOT NULL,
            profile_sha256 TEXT NOT NULL,
            repo_sha TEXT NOT NULL,
            area TEXT NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN ('structure', 'discover', 'verify')),
            unit_key TEXT NOT NULL,
            profile_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            complete INTEGER NOT NULL CHECK(complete IN (0, 1)),
            has_gaps INTEGER NOT NULL CHECK(has_gaps IN (0, 1)),
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL,
            PRIMARY KEY(run_id, phase, unit_key)
        );
        CREATE INDEX IF NOT EXISTS observations_repo_sha ON observations(repo_sha);
        CREATE INDEX IF NOT EXISTS observations_area ON observations(area);
        CREATE INDEX IF NOT EXISTS observations_profile ON observations(profile_sha256);
        CREATE INDEX IF NOT EXISTS observations_last_used ON observations(last_used_at);
        """
    )
    connection.commit()


def validate(connection: sqlite3.Connection, repo: str, kind: str) -> dict[str, Any]:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("SQLite integrity check failed")
    meta = metadata(connection)
    expected_version = RECORDS_SCHEMA_VERSION if kind == "records" else AUDIT_SCHEMA_VERSION
    if int(meta.get("schema_version", "-1")) != expected_version:
        raise ValueError("cache schema version is incompatible")
    if meta.get("repository") != normalized_repo(repo):
        raise ValueError("cache repository identity does not match")
    expected_kind = "records" if kind == "records" else "audit-history"
    if meta.get("database_kind") != expected_kind:
        raise ValueError("cache database kind does not match")
    count_query = (
        "SELECT COUNT(*) FROM records" if kind == "records" else "SELECT COUNT(*) FROM observations"
    )
    count = connection.execute(count_query).fetchone()[0]
    return {"metadata": meta, "count": count}


def live_path(directory: Path, kind: str) -> Path:
    return directory / (RECORDS_DB if kind == "records" else AUDIT_DB)


def prepare_database(args: argparse.Namespace, kind: str) -> None:
    run_id = validated_run_id(args.run_id)
    directory = repo_dir(args.cache_root, args.repo)
    secure_directory(directory)
    initializer = initialize_records if kind == "records" else initialize_audit
    live = live_path(directory, kind)
    if args.no_cache:
        temporary = Path(tempfile.mkdtemp(prefix=f"qwen-github-{kind}-", dir="/tmp"))
        os.chmod(temporary, 0o700)
        work = temporary / live.name
        with connect(work) as connection:
            initializer(connection, args.repo)
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
        stored_repo = None
        try:
            with connect(live) as current:
                stored_repo = metadata(current).get("repository")
        except (ValueError, sqlite3.DatabaseError):
            stored_repo = None
        if stored_repo is not None and stored_repo != normalized_repo(args.repo):
            raise ValueError(
                f"cache repository identity mismatch: the committed cache belongs to "
                f"{stored_repo}, not {normalized_repo(args.repo)}; refusing to move it aside"
            )
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
            if kind == "records":
                with connect(work) as connection, connection:
                    compact_existing_records(connection)
            mode = "reuse"
    if not reuse_live:
        with connect(work) as connection:
            initializer(connection, args.repo)
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


def json_list(value: Any) -> str:
    return json.dumps(value if isinstance(value, list) else [], sort_keys=True)


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
        linked_selected: set[tuple[str, int]] = set()
        for key in sorted(linked):
            row = connection.execute(
                "SELECT * FROM records WHERE kind=? AND number=?", key
            ).fetchone()
            if row:
                selected[key] = row
                linked_selected.add(key)
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
        included = {(row["kind"], row["number"]) for row in selected_records}
        linked_dropped = sum(1 for key in linked_selected if key not in included)
        result = {
            "cutoff": iso_utc(cutoff) if cutoff else None,
            "has_more": has_more,
            "linked_dropped": linked_dropped,
            "records": [row_dict(row) for row in selected_records],
        }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
        os.chmod(args.output, 0o600)
    else:
        print(rendered, end="")


def import_legacy(args: argparse.Namespace) -> None:
    imported = 0
    with connect(args.db) as target:
        validate(target, args.repo, "records")
        if args.legacy_db and args.legacy_db.exists():
            with sqlite3.connect(args.legacy_db) as legacy:
                legacy.row_factory = sqlite3.Row
                for row in legacy.execute("SELECT * FROM records"):
                    raw = dict(row)
                    raw["labels"] = json.loads(raw.pop("labels_json") or "[]")
                    raw["comments"] = json.loads(raw.pop("comments_json") or "[]")
                    raw["relationships"] = json.loads(raw.pop("relationships_json") or "{}")
                    item = normalize_record(
                        raw,
                        raw["kind"],
                        "legacy-curation",
                        raw.get("fetched_at") or iso_utc(utc_now()),
                    )
                    if not raw.get("full_content", 1):
                        item["hydration"] = "summary"
                    upsert_record(target, item)
                    imported += 1
        target.commit()
    print(json.dumps({"imported": imported}))


def canonical_profile(path: Path) -> tuple[dict[str, Any], str, str]:
    profile = json.loads(path.read_text())
    if not isinstance(profile, dict) or not isinstance(profile.get("repo_sha"), str):
        raise ValueError("profile JSON must be an object containing repo_sha")
    rendered = json.dumps(profile, sort_keys=True, separators=(",", ":"))
    return profile, rendered, hashlib.sha256(rendered.encode()).hexdigest()


def normalized_observation(value: Any, default_timestamp: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("each observation must be a JSON object")
    phase = value.get("phase")
    area = value.get("area")
    unit = value.get("unit")
    payload = value.get("payload")
    if phase not in OBSERVATION_PHASES:
        raise ValueError(f"observation phase must be one of {sorted(OBSERVATION_PHASES)}")
    if not isinstance(area, str) or not AREA_RE.fullmatch(area):
        raise ValueError("observation area must use area/<slug>")
    if not isinstance(unit, str) or not unit.strip():
        raise ValueError("observation unit must be a non-empty string")
    if not isinstance(payload, dict):
        raise ValueError("observation payload must be a JSON object")
    complete = value.get("complete", False)
    has_gaps = value.get("has_gaps", False)
    if not isinstance(complete, bool) or not isinstance(has_gaps, bool):
        raise ValueError("observation complete and has_gaps values must be booleans")
    timestamp = value.get("timestamp", default_timestamp)
    if not isinstance(timestamp, str) or parse_time(timestamp) is None:
        raise ValueError("observation timestamp must be an ISO-8601 string")
    return {
        "phase": phase,
        "area": area,
        "unit": unit,
        "payload": payload,
        "complete": complete,
        "has_gaps": has_gaps,
        "timestamp": timestamp,
    }


def write_observations(
    connection: sqlite3.Connection,
    run_id: str,
    profile: dict[str, Any],
    rendered_profile: str,
    profile_hash: str,
    observations: list[dict[str, Any]],
) -> tuple[int, int]:
    keys = [(item["phase"], item["unit"]) for item in observations]
    if len(keys) != len(set(keys)):
        raise ValueError("observation batch contains duplicate phase/unit keys")
    existing = {
        (row["phase"], row["unit_key"])
        for row in connection.execute(
            "SELECT phase, unit_key FROM observations WHERE run_id=?",
            (run_id,),
        )
    }
    for item in observations:
        rendered_payload = json.dumps(item["payload"], sort_keys=True, separators=(",", ":"))
        connection.execute(
            "INSERT INTO observations(run_id, profile_sha256, repo_sha, area, phase, unit_key, profile_json, payload_json, "
            "payload_sha256, complete, has_gaps, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, phase, unit_key) DO UPDATE SET profile_sha256=excluded.profile_sha256, "
            "repo_sha=excluded.repo_sha, area=excluded.area, profile_json=excluded.profile_json, payload_json=excluded.payload_json, "
            "payload_sha256=excluded.payload_sha256, complete=excluded.complete, has_gaps=excluded.has_gaps, "
            "created_at=excluded.created_at, last_used_at=excluded.last_used_at",
            (
                run_id,
                profile_hash,
                profile["repo_sha"],
                item["area"],
                item["phase"],
                item["unit"],
                rendered_profile,
                rendered_payload,
                hashlib.sha256(rendered_payload.encode()).hexdigest(),
                int(item["complete"]),
                int(item["has_gaps"]),
                item["timestamp"],
                item["timestamp"],
            ),
        )
    updated = sum(key in existing for key in keys)
    return len(keys) - updated, updated


def put_observations(args: argparse.Namespace) -> None:
    if args.input.stat().st_size > OBSERVATION_INPUT_BYTES:
        raise ValueError(f"observation input exceeds {OBSERVATION_INPUT_BYTES} bytes")
    raw = json.loads(args.input.read_text())
    if not isinstance(raw, list) or not raw:
        raise ValueError("observation input must be a non-empty JSON array")
    now = args.timestamp or iso_utc(utc_now())
    observations = [normalized_observation(value, now) for value in raw]
    run_id = validated_run_id(args.run_id)
    profile, rendered_profile, profile_hash = canonical_profile(args.profile)
    with connect(args.db) as connection:
        validate(connection, args.repo, "audit")
        inserted, updated = write_observations(
            connection, run_id, profile, rendered_profile, profile_hash, observations
        )
        connection.commit()
    print(
        json.dumps(
            {
                "profile_sha256": profile_hash,
                "inserted": inserted,
                "updated": updated,
                "observations": [
                    {"area": item["area"], "phase": item["phase"], "unit": item["unit"]}
                    for item in observations
                ],
            }
        )
    )


def query_observations(args: argparse.Namespace) -> None:
    clauses: list[str] = []
    parameters: list[Any] = []
    if args.area and not AREA_RE.fullmatch(args.area):
        raise ValueError("observation area must use area/<slug>")
    if args.run_id:
        clauses.append("run_id=?")
        parameters.append(validated_run_id(args.run_id))
    if args.repo_sha:
        clauses.append("repo_sha=?")
        parameters.append(args.repo_sha)
    if args.area:
        clauses.append("area=?")
        parameters.append(args.area)
    if args.phase:
        clauses.append("phase=?")
        parameters.append(args.phase)
    if args.unit:
        clauses.append("unit_key=?")
        parameters.append(args.unit)
    sql = "SELECT * FROM observations"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, run_id, phase, unit_key"
    if args.limit:
        sql += " LIMIT ?"
        parameters.append(args.limit)
    with connect(args.db) as connection:
        validate(connection, args.repo, "audit")
        rows: list[dict[str, Any]] = []
        touched: list[tuple[str, str, str]] = []
        for row in connection.execute(sql, parameters):
            item = dict(row)
            touched.append((item["run_id"], item["phase"], item["unit_key"]))
            item["payload"] = json.loads(item.pop("payload_json"))
            item["complete"] = bool(item["complete"])
            item["has_gaps"] = bool(item["has_gaps"])
            item.pop("profile_json", None)
            rows.append(item)
        timestamp = iso_utc(utc_now())
        connection.executemany(
            "UPDATE observations SET last_used_at=? WHERE run_id=? AND phase=? AND unit_key=?",
            [(timestamp, *key) for key in touched],
        )
        connection.commit()
    print(json.dumps({"observations": rows}, indent=2))


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


def prune_audit(
    connection: sqlite3.Connection, keep_shas: int, retention_days: int, now: dt.datetime
) -> int:
    newest = {
        row["repo_sha"]
        for row in connection.execute(
            "SELECT repo_sha, MAX(created_at) AS newest FROM observations GROUP BY repo_sha ORDER BY newest DESC LIMIT ?",
            (keep_shas,),
        )
    }
    cutoff = now - dt.timedelta(days=retention_days)
    removed = 0
    for row in connection.execute(
        "SELECT run_id, phase, unit_key, repo_sha, last_used_at FROM observations"
    ):
        used = parse_time(row["last_used_at"])
        if row["repo_sha"] in newest or (used is not None and used >= cutoff):
            continue
        connection.execute(
            "DELETE FROM observations WHERE run_id=? AND phase=? AND unit_key=?",
            (row["run_id"], row["phase"], row["unit_key"]),
        )
        removed += 1
    return removed


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
            if kind == "audit":
                removed = prune_audit(connection, args.keep_shas, args.retention_days, utc_now())
            updates = {
                "generation": str(live_generation + 1),
                "last_sync_at": args.synced_at,
            }
            updates["snapshot_sha" if kind == "audit" else "default_sha"] = (
                args.repo_sha if kind == "audit" else args.default_sha
            )
            if kind == "records":
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
    if kind == "records":
        command.add_argument("--default-sha", required=True)
        command.add_argument("--full-history-complete", action="store_true")
    else:
        command.add_argument("--repo-sha", required=True)
        command.add_argument("--keep-shas", type=int, default=5)
        command.add_argument("--retention-days", type=int, default=90)
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
