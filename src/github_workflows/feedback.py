"""Private, append-only workflow and instruction feedback storage."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import stat
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

MAX_RECORD_BYTES = 64 * 1024
MAX_FAILURE_BYTES = 32 * 1024
FAILURE_LIMIT = 128
FAILURE_TTL_SECONDS = 60 * 60
FAILURE_RESPONSE_BYTES = 16 * 1024
SAFE_FAILURE_ARGUMENTS = frozenset(
    {
        "action",
        "area",
        "candidate_id",
        "cutoff",
        "dry_run",
        "fetched_at",
        "full_history_complete",
        "kind",
        "limit",
        "mutation",
        "n",
        "probe_id",
        "refresh_history",
        "regression_sweep",
        "repository",
        "request_id",
        "separate",
        "source_confirmed",
        "state",
        "task_id",
        "task_ref",
        "workflow",
    }
)
SAFE_FAILURE_TOKEN = re.compile(r"[A-Za-z0-9._:/@+\-]{1,256}")
SECRET_KEY = re.compile(
    r"(?:authorization|cookie|credential|password|private[_-]?key|secret|token|api[_-]?key)",
    re.IGNORECASE,
)


def storage_path() -> Path:
    """Return the single user-local feedback JSONL path."""
    configured = os.environ.get("XDG_CACHE_HOME")
    cache = Path(configured).expanduser() if configured else Path.home() / ".cache"
    if not cache.is_absolute():
        raise ValueError("XDG_CACHE_HOME must be an absolute path")
    return cache / "agent-workflows" / "feedback.jsonl"


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
        records.append(value)
    return records


def read_records() -> list[dict[str, Any]]:
    """Read validated feedback without creating the cache."""
    path = storage_path()
    if not path.parent.exists() or not path.exists():
        return []
    metadata = path.parent.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise PermissionError("feedback cache must be an owned directory")
    with _locked(path, exclusive=False):
        return _read(path)


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


def _failure_arguments(
    arguments: Mapping[str, Any], replacements: list[tuple[str, str]]
) -> dict[str, Any]:
    """Retain only non-payload fields useful for identifying a failed operation."""
    retained: dict[str, Any] = {}
    for key, value in arguments.items():
        name = str(key)
        if name not in SAFE_FAILURE_ARGUMENTS:
            continue
        if value is None or isinstance(value, (bool, int, float)):
            retained[name] = value
        elif isinstance(value, str) and SAFE_FAILURE_TOKEN.fullmatch(value):
            retained[name] = _sanitize(value, replacements)
        else:
            retained[name] = f"<{type(value).__name__}>"
    return retained or {"omitted": "automatic failure arguments were not retained"}


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode()
    if len(encoded) <= limit:
        return value
    marker = "… <truncated>"
    prefix = encoded[: max(0, limit - len(marker.encode()))].decode(errors="ignore")
    return prefix + marker


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
        response: str,
        provenance: Mapping[str, Any] | None = None,
    ) -> str:
        """Return a short reference to one sanitized failure snapshot."""
        value = {
            "tool": tool,
            "arguments": _failure_arguments(arguments, self.replacements),
            "response": _truncate_utf8(
                _sanitize(response, self.replacements), FAILURE_RESPONSE_BYTES
            ),
            "provenance": _sanitize(dict(provenance or {}), self.replacements),
        }
        if _encoded_size(value) > MAX_FAILURE_BYTES:
            value["arguments"] = {"omitted": "automatic failure arguments exceeded limit"}
            value["provenance"] = {"omitted": "automatic failure provenance exceeded limit"}
        if _encoded_size(value) > MAX_FAILURE_BYTES:
            value["response"] = _truncate_utf8(str(value["response"]), 1024)
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
    arguments: dict[str, Any] | None,
    response: str | None,
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
        "feedback_id": f"fb-{uuid.uuid4().hex[:12]}",
        "timestamp": timestamp,
        "repository": repository,
        "workflow": workflow,
        "run_id": run_id,
        "message": _sanitize(message, replacements),
        "tool": tool,
        "arguments": _sanitize(arguments, replacements) if arguments is not None else None,
        "response": _sanitize(response, replacements) if response is not None else None,
    }
    sanitized_provenance = _sanitize(provenance, replacements) if provenance else None
    if sanitized_provenance:
        record["provenance"] = sanitized_provenance
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > MAX_RECORD_BYTES:
        raise ValueError("feedback context exceeds 64 KiB; provide a smaller relevant excerpt")

    path = storage_path()
    with _locked(path, exclusive=True):
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
    return {"recorded": True, "feedback_id": record["feedback_id"]}


def compact_records(
    *,
    repository: str | None = None,
    workflow: str | None = None,
    tool: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return newest matching records without bulky tool context."""
    records = [
        record
        for record in read_records()
        if (repository is None or record.get("repository") == repository)
        and (workflow is None or record.get("workflow") == workflow)
        and (tool is None or record.get("tool") == tool)
    ]
    return [
        {key: value for key, value in record.items() if key not in {"arguments", "response"}}
        for record in reversed(records[-limit:])
    ]


def find(feedback_id: str) -> dict[str, Any]:
    """Return one complete feedback record by exact ID or unique legacy suffix."""
    records = read_records()
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


def _display_id(value: Any) -> str:
    rendered = str(value or "-")
    legacy = re.fullmatch(r"fb-\d{14}-([0-9a-f]{10})", rendered)
    return legacy.group(1) if legacy else rendered


def _display_time(value: Any) -> str:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value or "-")
    return parsed.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%SZ")


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
    headers = ["ID", "WHEN (UTC)", "REPOSITORY", "CONTEXT", "SOURCE"]
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
                    _one_line(_display_id(record.get("feedback_id"))),
                    _one_line(_display_time(record.get("timestamp"))),
                    _one_line(record.get("repository")),
                    _one_line(context),
                    _one_line(record.get("tool") or "guidance"),
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
