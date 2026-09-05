"""Private workflow and instruction feedback storage."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import stat
import subprocess
import textwrap
import threading
import time
import unicodedata
import uuid
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

MAX_RECORD_BYTES = 8 * 1024
MAX_FAILURE_BYTES = 4 * 1024
FAILURE_LIMIT = 128
FAILURE_TTL_SECONDS = 60 * 60
SHORT_REF_LENGTH = 8
SAFE_SELECTOR_ARGUMENTS = frozenset({"action", "kind", "method", "workflow"})
SAFE_ARGUMENT_NAMES = frozenset(
    {
        "action",
        "activity",
        "area",
        "areas",
        "artifacts",
        "candidate",
        "candidate_id",
        "code",
        "cutoff",
        "dry_run",
        "fact",
        "facts",
        "fetched_at",
        "findings",
        "full_history_complete",
        "head_drift",
        "instructions",
        "kind",
        "limit",
        "limitation",
        "linked",
        "method",
        "n",
        "note",
        "operation",
        "pending",
        "phase",
        "probe_id",
        "programs",
        "receipt",
        "records",
        "refresh_history",
        "regression_sweep",
        "report",
        "repository",
        "request_id",
        "selectors",
        "separate",
        "shard",
        "source",
        "source_confirmed",
        "state",
        "target",
        "targets",
        "task",
        "task_id",
        "task_ref",
        "terms",
        "verdict",
        "versions",
        "workflow",
    }
)
SAFE_FAILURE_TOKEN = re.compile(r"[A-Za-z0-9._:+\-]{1,128}")
RESOLUTION_DISPOSITIONS = frozenset({"addressed", "duplicate", "not-actionable", "external"})
SECRET_KEY = re.compile(
    r"(?:authorization|cookie|credential|password|private[_-]?key|secret|token|api[_-]?key)",
    re.IGNORECASE,
)
FALLBACK_EDT = dt.timezone(dt.timedelta(hours=-4), name="EDT")
REMOTE_REPOSITORY = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.-]*://[^/]+/|[^/]+@[^:]+:)"
    r"([^/\s]+)/([^/\s]+?)(?:\.git)?"
)


def storage_path() -> Path:
    """Return the single user-local feedback JSONL path."""
    configured = os.environ.get("XDG_CACHE_HOME")
    cache = Path(configured).expanduser() if configured else Path.home() / ".cache"
    if not cache.is_absolute():
        raise ValueError("XDG_CACHE_HOME must be an absolute path")
    return cache / "agent-workflows" / "feedback.jsonl"


def repository_from_workspace(workspace: Path) -> str | None:
    """Return an owner/repository identity from a non-local origin URL."""
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    match = REMOTE_REPOSITORY.fullmatch(result.stdout.strip()) if result.returncode == 0 else None
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise PermissionError("feedback cache must be an owned directory")
    path.chmod(0o700)


def _private_file(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise PermissionError("feedback cache must be an owned regular file")
    path.chmod(0o600)


@contextmanager
def _locked(path: Path, *, exclusive: bool) -> Iterator[None]:
    _private_directory(path.parent)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path.parent / ".feedback.lock", flags, 0o600)
    try:
        lock_path = path.parent / ".feedback.lock"
        _private_file(lock_path)
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        os.close(descriptor)


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    _private_file(path)
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid feedback JSON on line {line_number}") from error
        if not isinstance(value, dict):
            raise ValueError(f"feedback line {line_number} must be an object")
        required = {"feedback_id", "timestamp", "message"}
        if not required.issubset(value):
            raise ValueError(f"feedback line {line_number} is missing required fields")
        if value.get("status", "open") not in {"open", "closed"}:
            raise ValueError(f"feedback line {line_number} has an invalid status")
        resolution = value.get("resolution")
        if resolution is not None and (
            not isinstance(resolution, dict)
            or resolution.get("disposition") not in RESOLUTION_DISPOSITIONS
            or (
                "note" in resolution
                and (not isinstance(resolution["note"], str) or len(resolution["note"]) > 500)
            )
        ):
            raise ValueError(f"feedback line {line_number} has an invalid resolution")
        records.append(value)
    return records


def read_records() -> list[dict[str, Any]]:
    """Read validated feedback, scrubbing legacy payload fields on first access."""
    path = storage_path()
    if not path.parent.exists() or not path.exists():
        return []
    metadata = path.parent.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise PermissionError("feedback cache must be an owned directory")
    with _locked(path, exclusive=True):
        records = _read(path)
        if _normalize_legacy_records(records):
            _rewrite(path, records)
        return records


def _sanitize(value: Any, replacements: list[tuple[str, str]]) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>" if SECRET_KEY.search(str(key)) else _sanitize(item, replacements)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item, replacements) for item in value]
    if isinstance(value, str):
        result = value
        for source, replacement in replacements:
            result = result.replace(source, replacement)
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _sanitize(str(value), replacements)


def _replacements(private_paths: list[tuple[Path, str]]) -> list[tuple[str, str]]:
    return sorted(
        ((str(path), replacement) for path, replacement in private_paths),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )


def _value_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def _safe_invocation(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Describe a call without retaining arbitrary values that may contain PHI or PII."""
    argument_types = {
        str(key): _value_kind(value)
        for key, value in arguments.items()
        if str(key) in SAFE_ARGUMENT_NAMES
    }
    selectors = {
        str(key): value
        for key, value in arguments.items()
        if str(key) in SAFE_SELECTOR_ARGUMENTS
        and isinstance(value, str)
        and SAFE_FAILURE_TOKEN.fullmatch(value)
    }
    omitted = sorted(set(argument_types) - set(selectors))
    result: dict[str, Any] = {
        "argument_types": argument_types,
        "complete": not omitted,
    }
    if selectors:
        result["selectors"] = selectors
    if omitted:
        result["omitted"] = omitted
    unknown_count = len(arguments) - len(argument_types)
    if unknown_count:
        result["unknown_argument_count"] = unknown_count
        result["complete"] = False
    return result


def _normalize_legacy_records(records: list[dict[str, Any]]) -> bool:
    """Remove raw legacy call payloads without requiring a store schema version."""
    changed = False
    for record in records:
        if "status" not in record:
            record["status"] = "open"
            changed = True
        arguments_present = "arguments" in record
        arguments = record.pop("arguments", None)
        response_present = "response" in record
        record.pop("response", None)
        if isinstance(arguments, Mapping) and "origin" not in record:
            record["origin"] = {
                "failure_kind": "legacy",
                "invocation": _safe_invocation(arguments),
            }
        if arguments_present or response_present:
            changed = True
    return changed


def _encoded_size(value: Mapping[str, Any]) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


class FailureRegistry:
    """Keep bounded, sanitized MCP failures available for optional feedback."""

    def __init__(
        self,
        private_paths: list[tuple[Path, str]],
        *,
        limit: int = FAILURE_LIMIT,
        ttl_seconds: float = FAILURE_TTL_SECONDS,
    ) -> None:
        self.replacements = _replacements(private_paths)
        self.limit = limit
        self.ttl_seconds = ttl_seconds
        self._items: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._items:
            reference, (created, _value) = next(iter(self._items.items()))
            if now - created <= self.ttl_seconds and len(self._items) <= self.limit:
                break
            self._items.pop(reference)

    def record(
        self,
        *,
        tool: str,
        arguments: Mapping[str, Any],
        failure_kind: str,
        provenance: Mapping[str, Any] | None = None,
    ) -> str:
        """Return a short reference to one sanitized failure snapshot."""
        value = {
            "tool": tool,
            "origin": {
                "failure_kind": failure_kind,
                "invocation": _safe_invocation(arguments),
            },
            "provenance": _sanitize(dict(provenance or {}), self.replacements),
        }
        if _encoded_size(value) > MAX_FAILURE_BYTES:
            value["provenance"] = {"omitted": "automatic failure provenance exceeded limit"}
        if _encoded_size(value) > MAX_FAILURE_BYTES:  # pragma: no cover - fixed fields are bounded
            raise ValueError("automatic failure snapshot exceeds internal limit")
        reference = f"err-{uuid.uuid4().hex[:12]}"
        now = time.monotonic()
        with self._lock:
            self._items[reference] = (now, value)
            self._prune(now)
        return reference

    def resolve(self, reference: str) -> dict[str, Any] | None:
        """Return one unexpired failure snapshot without consuming it."""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            item = self._items.get(reference)
            return deepcopy(item[1]) if item is not None else None


def append(
    *,
    message: str,
    tool: str | None,
    origin: dict[str, Any] | None,
    repository: str | None,
    workflow: str | None,
    run_id: str | None,
    private_paths: list[tuple[Path, str]],
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sanitize and append one feedback record."""
    timestamp = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    replacements = _replacements(private_paths)
    record: dict[str, Any] = {
        "timestamp": timestamp,
        "status": "open",
        "repository": repository,
        "workflow": workflow,
        "run_id": run_id,
        "message": _sanitize(message, replacements),
        "tool": tool,
    }
    sanitized_origin = _sanitize(origin, replacements) if origin else None
    if sanitized_origin:
        record["origin"] = sanitized_origin
    sanitized_provenance = _sanitize(provenance, replacements) if provenance else None
    if sanitized_provenance:
        record["provenance"] = sanitized_provenance
    path = storage_path()
    with _locked(path, exclusive=True):
        existing = _read(path) if path.exists() else []
        normalized = _normalize_legacy_records(existing)
        existing_ids = {str(item["feedback_id"]) for item in existing}
        while True:
            feedback_id = f"fb-{uuid.uuid4().hex[:12]}"
            short_ref = feedback_id[-SHORT_REF_LENGTH:]
            if feedback_id not in existing_ids and not any(
                existing_id.endswith(short_ref) for existing_id in existing_ids
            ):
                break
        record["feedback_id"] = feedback_id
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(encoded) > MAX_RECORD_BYTES:
            raise ValueError("feedback record exceeds 8 KiB; shorten the PHI-free summary")
        if normalized:
            _rewrite(path, existing)
        flags = os.O_CREAT | os.O_RDWR | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            _private_file(path)
            original_size = os.fstat(descriptor).st_size
            try:
                view = memoryview(encoded)
                while view:
                    written = os.write(descriptor, view)
                    if written < 1:
                        raise OSError("feedback append made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            except BaseException:
                os.ftruncate(descriptor, original_size)
                os.fsync(descriptor)
                raise
        finally:
            os.close(descriptor)
    return {
        "recorded": True,
        "feedback_id": record["feedback_id"],
        "ref": feedback_ref(str(record["feedback_id"]), [*existing, record]),
    }


def append_manual(*, message: str, tool: str | None, workspace: Path) -> dict[str, Any]:
    """Record one CLI observation with mechanically derived local context."""
    resolved_workspace = workspace.resolve()
    return append(
        message=message,
        tool=tool,
        origin={"failure_kind": "manual"},
        repository=repository_from_workspace(resolved_workspace),
        workflow=None,
        run_id=None,
        provenance={"client": {"name": "agent-workflows-cli"}},
        private_paths=[
            (resolved_workspace, "<workspace>"),
            (storage_path().parent, "<feedback-cache>"),
            (Path.home(), "<home>"),
        ],
    )


def compact_records(
    *,
    repository: str | None = None,
    workflow: str | None = None,
    sources: list[str] | None = None,
    closed: bool = False,
    status: str | None = None,
    cutoff: str | None = None,
    limit: int | None = 50,
) -> list[dict[str, Any]]:
    """Return newest matching records with globally resolvable short references."""
    selected_status = status or ("closed" if closed else "open")
    if selected_status not in {"open", "closed", "all"}:
        raise ValueError("feedback status must be open, closed, or all")
    if limit is not None and limit < 1:
        raise ValueError("feedback limit must be positive")
    cutoff_time = _parse_cutoff(cutoff)
    requested_sources = {source.casefold() for source in sources or []}
    records = read_records()
    matching = [
        record
        for record in records
        if (repository is None or record.get("repository") == repository)
        and (workflow is None or record.get("workflow") == workflow)
        and (selected_status == "all" or record.get("status", "open") == selected_status)
        and (cutoff_time is None or _record_time(record) >= cutoff_time)
        and (
            not requested_sources
            or source_name(record).casefold() in requested_sources
            or str(record.get("tool") or "").casefold() in requested_sources
        )
    ]
    selected = list(reversed(matching if limit is None else matching[-limit:]))
    return [
        {
            **record,
            "ref": feedback_ref(str(record["feedback_id"]), records),
            "source": source_name(record),
        }
        for record in selected
    ]


def feedback_ref(feedback_id: str, records: list[dict[str, Any]]) -> str:
    """Return the shortest collision-free display suffix, preferring eight characters."""
    for length in range(SHORT_REF_LENGTH, len(feedback_id) + 1):
        suffix = feedback_id[-length:]
        if sum(str(record.get("feedback_id", "")).endswith(suffix) for record in records) == 1:
            return suffix
    return feedback_id


def _parse_feedback_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _parse_cutoff(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    parsed = _parse_feedback_time(value)
    if parsed is None:
        raise ValueError("feedback cutoff must be an ISO-8601 timestamp")
    return parsed


def _record_time(record: Mapping[str, Any]) -> dt.datetime:
    parsed = _parse_feedback_time(record.get("timestamp"))
    if parsed is None:
        raise ValueError("feedback record has an invalid timestamp")
    return parsed


def feedback_summary(
    *,
    repository: str | None = None,
    workflow: str | None = None,
    cutoff: str | None = None,
) -> dict[str, Any]:
    """Return aggregate feedback state for one repository, workflow, and time scope."""
    cutoff_time = _parse_cutoff(cutoff)
    records = read_records()
    scoped = [
        record
        for record in records
        if (repository is None or record.get("repository") == repository)
        and (workflow is None or record.get("workflow") == workflow)
        and (cutoff_time is None or _record_time(record) >= cutoff_time)
    ]
    open_sources: dict[str, int] = {}
    closed_sources: dict[str, dict[str, Any]] = {}
    for record in scoped:
        source = source_name(record)
        status = str(record.get("status", "open"))
        if status == "open":
            open_sources[source] = open_sources.get(source, 0) + 1
            continue
        resolution = record.get("resolution")
        disposition = (
            str(resolution.get("disposition"))
            if isinstance(resolution, dict) and resolution.get("disposition")
            else "unspecified"
        )
        source_summary = closed_sources.setdefault(source, {"records": 0, "dispositions": {}})
        source_summary["records"] += 1
        source_dispositions = source_summary["dispositions"]
        source_dispositions[disposition] = source_dispositions.get(disposition, 0) + 1
    timestamps = sorted(_record_time(record) for record in scoped)
    sizes = [_encoded_size(record) for record in scoped]
    return {
        "scope": {
            "repository": repository,
            "workflow": workflow,
            "cutoff": _iso_utc(cutoff_time) if cutoff_time is not None else None,
        },
        "open": {
            "records": sum(open_sources.values()),
            "sources": [
                {"source": source, "records": count}
                for source, count in sorted(
                    open_sources.items(), key=lambda item: (-item[1], item[0])
                )
            ],
        },
        "closed": {
            "records": sum(item["records"] for item in closed_sources.values()),
            "sources": [
                {
                    "source": source,
                    "records": values["records"],
                    "dispositions": [
                        {"disposition": disposition, "records": count}
                        for disposition, count in sorted(
                            values["dispositions"].items(),
                            key=lambda item: (-item[1], item[0]),
                        )
                    ],
                }
                for source, values in sorted(
                    closed_sources.items(), key=lambda item: (-item[1]["records"], item[0])
                )
            ],
        },
        "range": {
            "oldest": _iso_utc(timestamps[0]) if timestamps else None,
            "newest": _iso_utc(timestamps[-1]) if timestamps else None,
        },
        "storage": {
            "bytes": sum(sizes),
            "average_record_bytes": round(sum(sizes) / len(sizes)) if sizes else 0,
            "largest_record_bytes": max(sizes, default=0),
        },
    }


def source_name(record: dict[str, Any]) -> str:
    """Return the compact logical source name for one feedback record."""
    tool = record.get("tool")
    if not isinstance(tool, str) or not tool:
        return "general"
    if tool.startswith("mcp__") and "__" in tool[5:]:
        return tool.rsplit("__", 1)[-1]
    return tool


def _qwen_home() -> Path:
    configured = os.environ.get("QWEN_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".qwen"
    if not home.is_absolute():
        raise ValueError("QWEN_HOME must be an absolute path")
    return home


def _candidate_transcripts(feedback_id: str, root: Path) -> list[Path]:
    projects = root / "projects"
    if not projects.is_dir():
        return []
    try:
        result = subprocess.run(
            [
                "rg",
                "--hidden",
                "--no-ignore",
                "-l",
                "--fixed-strings",
                "--",
                feedback_id,
                str(projects),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        matches = []
        for path in projects.rglob("*.jsonl"):
            if any(part in {"chats", "subagents"} for part in path.parts):
                try:
                    if feedback_id in path.read_text(encoding="utf-8", errors="replace"):
                        matches.append(path)
                except OSError:
                    continue
        return matches
    if result.returncode not in {0, 1}:
        raise RuntimeError("could not search Qwen transcripts")
    return [
        Path(line)
        for line in result.stdout.splitlines()
        if line and any(part in {"chats", "subagents"} for part in Path(line).parts)
    ]


def _parts(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    parts = message.get("parts") if isinstance(message, Mapping) else None
    return [part for part in parts or [] if isinstance(part, dict)]


def _call_from_parts(record: Mapping[str, Any], call_id: str) -> dict[str, Any] | None:
    for part in _parts(record):
        call = part.get("functionCall")
        if isinstance(call, dict) and call.get("id") == call_id:
            return call
    return None


def _result_from_parts(record: Mapping[str, Any]) -> dict[str, Any] | None:
    for part in _parts(record):
        result = part.get("functionResponse")
        if isinstance(result, dict):
            return result
    return None


def _result_feedback_id(result: Mapping[str, Any]) -> str | None:
    response = result.get("response")
    output = response.get("output") if isinstance(response, Mapping) else None
    if not isinstance(output, str):
        return None
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return None
    value = payload.get("feedback_id") if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None


def _normalize_tool_name(name: Any) -> str:
    rendered = str(name or "")
    return rendered.rsplit("__", 1)[-1] if rendered.startswith("mcp__") else rendered


def _origin_trace(
    transcript: list[dict[str, Any]],
    result_index: int,
    *,
    error_ref: str | None,
    tool: str | None,
) -> dict[str, Any] | None:
    for item in reversed(transcript[:result_index]):
        result = _result_from_parts(item)
        if result is None:
            continue
        name = result.get("name")
        response = result.get("response")
        call_id = result.get("id")
        if name == "mcp__github_workflows__workflow_feedback":
            continue
        if error_ref is not None and error_ref in json.dumps(response, sort_keys=True):
            return {
                "match": "exact-error-ref",
                "tool": name,
                "tool_call_id": call_id,
            }
        if (
            error_ref is None
            and tool is not None
            and _normalize_tool_name(name) == _normalize_tool_name(tool)
        ):
            return {"match": "nearest-tool", "tool": name, "tool_call_id": call_id}
    return None


def _read_transcript(path: Path) -> list[dict[str, Any]]:
    """Read valid JSON objects while tolerating partial concurrent JSONL rows."""
    transcript: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                transcript.append(value)
    return transcript


def trace(feedback_id: str) -> dict[str, Any]:
    """Locate the feedback call in Qwen transcripts without returning conversation data."""
    record = find(feedback_id)
    actual_id = str(record["feedback_id"])
    root = _qwen_home()
    matches: list[dict[str, Any]] = []
    for path in _candidate_transcripts(actual_id, root):
        try:
            transcript = _read_transcript(path)
        except OSError:
            continue
        for index, item in enumerate(transcript):
            result = _result_from_parts(item)
            if (
                result is None
                or result.get("name") != "mcp__github_workflows__workflow_feedback"
                or _result_feedback_id(result) != actual_id
            ):
                continue
            call_id = str(result.get("id") or "")
            feedback_call = next(
                (
                    call
                    for prior in reversed(transcript[:index])
                    if (call := _call_from_parts(prior, call_id)) is not None
                ),
                None,
            )
            call_arguments = feedback_call.get("args") if feedback_call else None
            error_ref = (
                call_arguments.get("error_ref")
                if isinstance(call_arguments, Mapping)
                and isinstance(call_arguments.get("error_ref"), str)
                else None
            )
            if error_ref is None:
                origin = record.get("origin")
                error_ref = (
                    origin.get("error_ref")
                    if isinstance(origin, Mapping) and isinstance(origin.get("error_ref"), str)
                    else None
                )
            try:
                relative = path.relative_to(root)
                display_path = (
                    f"$QWEN_HOME/{relative}"
                    if os.environ.get("QWEN_HOME")
                    else f"~/.qwen/{relative}"
                )
            except ValueError:
                display_path = str(path)
            match: dict[str, Any] = {
                "timestamp": item.get("timestamp"),
                "session_id": item.get("sessionId"),
                "agent_id": item.get("agentId"),
                "feedback_tool_call_id": call_id or None,
                "transcript": display_path,
            }
            origin_trace = _origin_trace(
                transcript,
                index,
                error_ref=error_ref,
                tool=record.get("tool") if isinstance(record.get("tool"), str) else None,
            )
            if origin_trace is not None:
                match["origin"] = origin_trace
            matches.append({key: value for key, value in match.items() if value is not None})
    if not matches:
        raise ValueError("feedback was not found in Qwen transcripts")
    return {"feedback_id": actual_id, "matches": matches}


def find(feedback_id: str) -> dict[str, Any]:
    """Return one complete feedback record by exact ID or unique suffix."""
    records = read_records()
    return _find_record(records, feedback_id)


def find_many(feedback_ids: list[str]) -> list[dict[str, Any]]:
    """Return complete feedback records in requested order from one store read."""
    if not feedback_ids:
        raise ValueError("at least one feedback ID is required")
    records = read_records()
    return [_find_record(records, value) for value in feedback_ids]


def _find_record(records: list[dict[str, Any]], feedback_id: str) -> dict[str, Any]:
    record = next((item for item in records if item.get("feedback_id") == feedback_id), None)
    if record is not None:
        return record
    matches = [
        item
        for item in records
        if len(feedback_id) >= 6 and str(item.get("feedback_id", "")).endswith(feedback_id)
    ]
    if not matches:
        raise ValueError("feedback ID was not found")
    if len(matches) > 1:
        raise ValueError("feedback ID suffix is ambiguous")
    return matches[0]


def _rewrite(path: Path, records: list[dict[str, Any]]) -> None:
    encoded = b"".join(
        (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        for record in records
    )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.new")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("feedback rewrite made no progress")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    _private_file(path)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory = os.open(path.parent, directory_flags)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _reject_conflicting_close(record: dict[str, Any], resolution: dict[str, str]) -> None:
    """Shared closed-record rule: re-closing under a different resolution needs a reopen."""
    if record.get("status", "open") == "closed" and record.get("resolution") != resolution:
        raise ValueError("closed feedback has a different resolution; reopen it first")


def resolve_records(resolutions: Any) -> dict[str, Any]:
    """Atomically close records with independently specified dispositions."""
    if not isinstance(resolutions, list) or not resolutions:
        raise ValueError("resolutions must be a non-empty array")
    normalized_resolutions: list[dict[str, str]] = []
    for item in resolutions:
        if not isinstance(item, dict) or set(item) - {"ref", "disposition", "note"}:
            raise ValueError("each resolution requires ref and disposition, with optional note")
        reference = item.get("ref")
        disposition = item.get("disposition")
        note = item.get("note")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("resolution ref must be a non-empty string")
        if disposition not in RESOLUTION_DISPOSITIONS:
            raise ValueError("feedback disposition is invalid")
        resolution = {"ref": reference.strip(), "disposition": str(disposition)}
        if note is not None:
            if not isinstance(note, str) or not note.strip():
                raise ValueError("feedback resolution note must not be blank")
            if len(note.strip()) > 500:
                raise ValueError("feedback resolution note must be at most 500 characters")
            resolution["note"] = note.strip()
        normalized_resolutions.append(resolution)

    path = storage_path()
    if not path.is_file():
        raise ValueError("feedback ID was not found")
    with _locked(path, exclusive=True):
        records = _read(path)
        legacy_normalized = _normalize_legacy_records(records)
        selected: list[tuple[dict[str, Any], dict[str, str], dict[str, str]]] = []
        selected_ids: set[str] = set()
        for item in normalized_resolutions:
            record = _find_record(records, item["ref"])
            feedback_id = str(record["feedback_id"])
            if feedback_id in selected_ids:
                raise ValueError("feedback resolution contains a duplicate record")
            selected_ids.add(feedback_id)
            resolution = {"disposition": item["disposition"]}
            if "note" in item:
                resolution["note"] = item["note"]
            _reject_conflicting_close(record, resolution)
            selected.append((record, item, resolution))

        timestamp = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        results: list[dict[str, Any]] = []
        changed = False
        for record, item, resolution in selected:
            item_changed = not (
                record.get("status", "open") == "closed" and record.get("resolution") == resolution
            )
            if item_changed:
                record["status"] = "closed"
                record["closed_at"] = timestamp
                record["resolution"] = resolution
                changed = True
            results.append(
                {
                    "ref": feedback_ref(str(record["feedback_id"]), records),
                    "feedback_id": record["feedback_id"],
                    "disposition": item["disposition"],
                    "changed": item_changed,
                }
            )
        if changed or legacy_normalized:
            _rewrite(path, records)
    return {
        "changed": sum(bool(item["changed"]) for item in results),
        "unchanged": sum(not bool(item["changed"]) for item in results),
        "resolved": results,
    }


def set_closed(
    feedback_ids: list[str],
    *,
    closed: bool,
    disposition: str = "addressed",
    note: str | None = None,
) -> list[str]:
    """Atomically close or reopen selected feedback records."""
    if not feedback_ids:
        raise ValueError("at least one feedback ID is required")
    path = storage_path()
    if not path.is_file():
        raise ValueError("feedback ID was not found")
    if disposition not in RESOLUTION_DISPOSITIONS:
        raise ValueError("feedback disposition is invalid")
    if note is not None:
        note = note.strip()
        if not note:
            raise ValueError("feedback resolution note must not be blank")
        if len(note) > 500:
            raise ValueError("feedback resolution note must be at most 500 characters")
    with _locked(path, exclusive=True):
        records = _read(path)
        normalized = _normalize_legacy_records(records)
        selected = [_find_record(records, value) for value in dict.fromkeys(feedback_ids)]
        status = "closed" if closed else "open"
        timestamp = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        resolution = {"disposition": disposition}
        if note is not None:
            resolution["note"] = note
        changed: list[dict[str, Any]] = []
        for record in selected:
            if closed:
                _reject_conflicting_close(record, resolution)
            expected_resolution = resolution if closed else None
            if (
                record.get("status", "open") == status
                and record.get("resolution") == expected_resolution
            ):
                continue
            record["status"] = status
            if closed:
                record["closed_at"] = timestamp
                record["resolution"] = resolution
            else:
                record.pop("closed_at", None)
                record.pop("resolution", None)
            changed.append(record)
        if changed or normalized:
            _rewrite(path, records)
    return [str(record["feedback_id"]) for record in changed]


def remove(feedback_ids: list[str]) -> list[str]:
    """Atomically remove explicitly selected feedback records."""
    if not feedback_ids:
        raise ValueError("at least one feedback ID is required")
    path = storage_path()
    if not path.is_file():
        raise ValueError("feedback ID was not found")
    with _locked(path, exclusive=True):
        records = _read(path)
        _normalize_legacy_records(records)
        selected = [_find_record(records, value) for value in dict.fromkeys(feedback_ids)]
        selected_ids = list(dict.fromkeys(str(record["feedback_id"]) for record in selected))
        selected_set = set(selected_ids)
        retained = [record for record in records if record.get("feedback_id") not in selected_set]
        _rewrite(path, retained)
    return selected_ids


def _display_time(value: Any) -> str:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value or "-")
    try:
        localized = parsed.astimezone()
        if localized.tzinfo is None or not localized.tzname():
            raise ValueError("system timezone is unavailable")
    except (OSError, ValueError):
        localized = parsed.astimezone(FALLBACK_EDT)
    return localized.strftime("%Y-%m-%d %H:%M:%S %Z")


def _one_line(value: Any) -> str:
    raw = str(value or "-")
    visible = "".join(
        (
            " "
            if character in "\r\n\t"
            else (
                f"\\x{ord(character):02x}" if ord(character) <= 0xFF else f"\\u{ord(character):04x}"
            )
            if unicodedata.category(character) in {"Cc", "Cf"}
            else character
        )
        for character in raw
    )
    return " ".join(visible.split())


def _bounded(value: Any, width: int) -> str:
    rendered = _one_line(value)
    return rendered if len(rendered) <= width else rendered[: max(1, width - 1)] + "…"


def _metadata_widths(headers: list[str], rows: list[list[str]], width: int) -> list[int]:
    """Size columns to their content, shrinking payload columns only when needed."""
    widths = [
        max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)
    ]
    minimums = [len(header) for header in headers]
    overflow = max(0, sum(widths) + 2 * (len(widths) - 1) - width)
    for group in ((2, 3, 4), (0, 1)):
        while overflow:
            active = [index for index in group if widths[index] > minimums[index]]
            if not active:
                break
            share = max(1, (overflow + len(active) - 1) // len(active))
            for index in active:
                reduction = min(share, widths[index] - minimums[index], overflow)
                widths[index] -= reduction
                overflow -= reduction
                if not overflow:
                    break
    return widths


def format_table(records: list[dict[str, Any]], *, width: int) -> str:
    """Render compact metadata followed by each complete wrapped summary."""
    if not records:
        return "No feedback recorded."
    width = max(width, 100)
    headers = ["ID", "WHEN (LOCAL)", "REPOSITORY", "CONTEXT", "SOURCE"]
    rows: list[tuple[list[str], str]] = []
    for record in records:
        provenance = record.get("provenance")
        task = provenance.get("task") if isinstance(provenance, dict) else None
        task_id = task.get("id") if isinstance(task, dict) else None
        workflow = record.get("workflow")
        context = (
            str(task_id)
            if width < 120 and task_id
            else "/".join(str(item) for item in (workflow, task_id) if item) or "-"
        )
        rows.append(
            (
                [
                    _one_line(record.get("ref") or record.get("feedback_id")),
                    _one_line(_display_time(record.get("timestamp"))),
                    _one_line(record.get("repository")),
                    _one_line(context),
                    _one_line(source_name(record)),
                ],
                _one_line(record.get("message")),
            )
        )
    widths = _metadata_widths(headers, [metadata for metadata, _summary in rows], width)
    rendered = [
        "  ".join(
            _bounded(value, size).ljust(size) for value, size in zip(headers, widths, strict=True)
        ),
        "  ".join("-" * size for size in widths),
    ]
    summary_prefix = "  Summary: "
    summary_indent = " " * len(summary_prefix)
    for metadata, summary in rows:
        rendered.append(
            "  ".join(
                _bounded(value, size).ljust(size)
                for value, size in zip(metadata, widths, strict=True)
            ).rstrip()
        )
        wrapped = textwrap.wrap(
            summary,
            width=max(20, width - len(summary_prefix)),
            break_long_words=False,
            break_on_hyphens=False,
        ) or [""]
        rendered.append(summary_prefix + wrapped[0])
        rendered.extend(summary_indent + line for line in wrapped[1:])
        rendered.append("")
    rendered.pop()
    return "\n".join(line.rstrip() for line in rendered)


def format_feedback_summary(summary: Mapping[str, Any]) -> str:
    """Render aggregate feedback state without including record messages."""
    scope = summary["scope"]
    open_summary = summary["open"]
    closed_summary = summary["closed"]
    storage = summary["storage"]
    time_range = summary["range"]
    lines = [
        f"Records: {open_summary['records'] + closed_summary['records']} total",
        f"Range: {_display_time(time_range['oldest'])} to {_display_time(time_range['newest'])}",
        f"Storage: {storage['bytes']} bytes; {storage['average_record_bytes']} average, "
        f"{storage['largest_record_bytes']} largest",
    ]
    selected_scope = [f"{name}={value}" for name, value in scope.items() if value is not None]
    if selected_scope:
        lines.insert(0, f"Scope: {', '.join(selected_scope)}")
    open_sources = open_summary["sources"]
    open_width = max([len("SOURCE"), *(len(str(item["source"])) for item in open_sources)])
    lines.extend(("", f"Open ({open_summary['records']})", f"{'SOURCE'.ljust(open_width)}  COUNT"))
    lines.append(f"{'-' * open_width}  -----")
    lines.extend(
        f"{str(item['source']).ljust(open_width)}  {str(item['records']).rjust(5)}"
        for item in open_sources
    )
    closed_sources = closed_summary["sources"]
    closed_width = max([len("SOURCE"), *(len(str(item["source"])) for item in closed_sources)])
    lines.extend(
        (
            "",
            f"Closed ({closed_summary['records']})",
            f"{'SOURCE'.ljust(closed_width)}  COUNT  DISPOSITIONS",
            f"{'-' * closed_width}  -----  ------------",
        )
    )
    for item in closed_sources:
        dispositions = ", ".join(
            f"{entry['disposition']}={entry['records']}" for entry in item["dispositions"]
        )
        lines.append(
            f"{str(item['source']).ljust(closed_width)}  "
            f"{str(item['records']).rjust(5)}  {dispositions}"
        )
    return "\n".join(lines)


def format_trace(result: Mapping[str, Any]) -> str:
    """Render transcript locators without conversation content."""
    lines = [f"Feedback: {_one_line(result['feedback_id'])}"]
    for index, match in enumerate(result.get("matches", []), 1):
        if index > 1:
            lines.append("")
        lines.extend(
            (
                f"Transcript: {_one_line(match.get('transcript', '-'))}",
                f"Session: {_one_line(match.get('session_id', '-'))}",
                f"Agent: {_one_line(match.get('agent_id', '-'))}",
                f"Feedback call: {_one_line(match.get('feedback_tool_call_id', '-'))}",
                f"Recorded: {_one_line(_display_time(match.get('timestamp')))}",
            )
        )
        origin = match.get("origin")
        if isinstance(origin, Mapping):
            lines.append(
                "Origin: "
                f"{_one_line(origin.get('tool', '-'))} "
                f"{_one_line(origin.get('tool_call_id', '-'))} "
                f"({_one_line(origin.get('match', '-'))})"
            )
    return "\n".join(lines)
