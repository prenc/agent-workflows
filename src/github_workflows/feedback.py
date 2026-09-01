"""Private, append-only feedback storage for workflow agents."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MAX_RECORD_BYTES = 64 * 1024
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
) -> dict[str, Any]:
    """Sanitize and append one feedback record."""
    timestamp = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
    replacements = sorted(
        ((str(path), replacement) for path, replacement in private_paths),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    record: dict[str, Any] = {
        "feedback_id": "",
        "timestamp": timestamp,
        "repository": repository,
        "workflow": workflow,
        "run_id": run_id,
        "message": _sanitize(message, replacements),
        "tool": tool,
        "arguments": _sanitize(arguments, replacements) if arguments is not None else None,
        "response": _sanitize(response, replacements) if response is not None else None,
    }
    compact_time = re.sub(r"[^0-9]", "", timestamp)[:14]
    record["feedback_id"] = f"fb-{compact_time}-{uuid.uuid4().hex[:10]}"
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
    """Return one complete feedback record by identifier."""
    record = next((item for item in read_records() if item.get("feedback_id") == feedback_id), None)
    if record is None:
        raise ValueError("feedback ID was not found")
    return record
