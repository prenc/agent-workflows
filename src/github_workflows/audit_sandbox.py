#!/usr/bin/env python3
"""Shared sandbox primitives for bounded, isolated audit child processes."""

from __future__ import annotations

import ctypes
import os
import re
import resource
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

# Deliberate GiB-scale virtual-memory ceiling for sandbox children: a probe
# exhausting it fails with a normal non-zero exit instead of threatening the
# host with an out-of-memory event.
ADDRESS_SPACE_BYTES = 8 * 1024 * 1024 * 1024
# Deliberate GiB-scale scratch ceiling for sandbox children: a size-bounded
# tmpfs over the scratch root makes a child that keeps creating FSIZE-capped
# files fail with ENOSPC and a normal non-zero exit instead of exhausting the
# host volume (Linux has no disk rlimit). tmpfs is demand-allocated, so the
# ceiling also bounds the host RAM a child can pin.
SCRATCH_BYTES = 2 * 1024 * 1024 * 1024
# Host-relative RLIMIT_NPROC policy: the live task count for our real UID
# plus a margin, floored so a small audit host keeps a sane spawn budget.
# Enforced per user namespace, so the bound is a safe superset inside the
# probe's namespace.
NPROC_MARGIN = 64
NPROC_FLOOR = 256
KILL_GRACE_SECONDS = 5
_NICE_ADJUSTMENT = 10

_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_LANDLOCK_WRITE_ACCESS = sum(1 << bit for bit in (1, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14))


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]


_MOUNT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")

# All mounts happen inside a single forked work subshell: on this kernel
# line PID 1 of a --pid namespace cannot fork again once its first child has
# died, so the keeper shell (PID 1) forks exactly once and then only waits.
# The work subshell execs the probe, so the probe itself is never PID 1 and
# keeps ordinary signal semantics. Read-only binds protect known audit state;
# Landlock independently denies filesystem writes outside probe scratch; a
# size-bounded tmpfs over the scratch root caps the total bytes the child can
# persist under scratch, so exhaustion is a child ENOSPC, not host-volume
# exhaustion.
_NAMESPACE_SCRIPT = (
    "set -eu\n"
    "(\n"
    '  worktree="$1"\n'
    '  scratch="$2"\n'
    '  scratch_bytes="$3"\n'
    '  working_directory="$4"\n'
    '  helper="$5"\n'
    '  count="$6"\n'
    "  shift 6\n"
    '  while [ "$count" -gt 0 ]; do\n'
    '    root="$1"\n'
    "    shift\n"
    "    count=$((count - 1))\n"
    '    /usr/bin/mount --bind "$root" "$root"\n'
    '    /usr/bin/mount -o remount,bind,ro "$root"\n'
    "  done\n"
    '  count="$1"\n'
    "  shift\n"
    '  while [ "$count" -gt 0 ]; do\n'
    '    root="$1"\n'
    "    shift\n"
    "    count=$((count - 1))\n"
    '    { /usr/bin/mount --bind "$root" "$root" '
    '      && /usr/bin/mount -o remount,bind,ro "$root"; } 2>/dev/null || true\n'
    "  done\n"
    "  # The parent-created environment subdirectories (home/cache/tmp) must\n"
    "  # exist inside the fresh tmpfs; the parent-opened stdout/stderr inodes\n"
    "  # stay on the host volume, so both capture paths keep working.\n"
    "  subdirs=\n"
    '  for entry in "$scratch"/*; do\n'
    '    [ -d "$entry" ] || continue\n'
    '    subdirs="$subdirs ${entry##*/}"\n'
    "  done\n"
    '  /usr/bin/mount -t tmpfs -o size="$scratch_bytes",mode=0700 tmpfs "$scratch"\n'
    "  for name in $subdirs; do\n"
    '    mkdir -p "$scratch/$name"\n'
    "  done\n"
    '  cd "$working_directory"\n'
    '  exec /usr/bin/python3 "$helper" --restrict-writes "$scratch" "$@"\n'
    ") &\n"
    "work=$!\n"
    "trap '' TERM INT\n"
    'wait "$work"\n'
    'exit "$?"\n'
)


def unescape_mount_field(value: str) -> str:
    """Decode the kernel's octal escapes in /proc/self/mounts fields."""
    return _MOUNT_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), value)


def mount_table() -> list[Path]:
    """Mount points from /proc/self/mounts in mount order (last = topmost)."""
    mounts: list[Path] = []
    try:
        lines = Path("/proc/self/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return mounts
    for line in lines:
        fields = line.split()
        if len(fields) >= 2:
            mounts.append(Path(unescape_mount_field(fields[1])))
    return mounts


def mount_point(path: Path) -> Path:
    """The topmost mount point containing ``path`` (itself if it is one)."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    best: Path | None = None
    best_order = -1
    for order, mount in enumerate(mount_table()):
        try:
            resolved.relative_to(mount)
        except ValueError:
            continue
        if (
            best is None
            or len(mount.parts) > len(best.parts)
            or (len(mount.parts) == len(best.parts) and order > best_order)
        ):
            best, best_order = mount, order
    return best if best is not None else Path("/")


def readonly_binds(
    worktree: Path,
    roots: Sequence[Path | None],
    scratch: Path | None = None,
) -> tuple[list[Path], list[Path]]:
    """Order read-only bind targets for the sandbox script.

    Returns ``(specific, mountpoints)``. Specific roots come first (deepest
    first, so nested paths stay reachable through the topmost mount) and the
    mount points those roots live on come last (deepest first, applied
    strictly after every specific bind underneath them). Binding a mount
    point read-only adds defense in depth for everything under it that the
    probe can reach. Scratch's own mount is skipped because it must remain
    writable; Landlock provides the comprehensive write boundary.
    """
    specific: list[Path] = []
    seen: set[Path] = set()
    scratch_resolved: Path | None = None
    if scratch is not None:
        try:
            scratch_resolved = scratch.resolve()
        except OSError:
            pass

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            candidate = path.resolve()
        except OSError:
            return
        if scratch_resolved is not None:
            try:
                scratch_resolved.relative_to(candidate)
            except ValueError:
                pass
            else:
                # A strict read-only bind here would make the probe's own
                # scratch unwritable; the specific roots protect audit state,
                # the scratch must stay writable.
                return
        if candidate not in seen:
            seen.add(candidate)
            specific.append(candidate)

    add(worktree)
    for root in roots:
        add(root)
    specific.sort(key=lambda path: len(path.parts), reverse=True)
    scratch_mount = mount_point(scratch) if scratch is not None else None
    mountpoints: list[Path] = []
    mounts_seen: set[Path] = set()
    for path in specific:
        mount = mount_point(path)
        if mount == scratch_mount or mount in mounts_seen or mount in seen:
            continue
        mounts_seen.add(mount)
        mountpoints.append(mount)
    mountpoints.sort(key=lambda path: len(path.parts), reverse=True)
    return specific, mountpoints


def interpreter_prefix(python: Path) -> Path | None:
    """The directory prefix holding an interpreter's base installation.

    For a venv interpreter this is the base CPython prefix reached through
    the venv's python symlink; for a bare executable it is the prefix
    directory itself.
    """
    try:
        resolved = python.resolve()
    except OSError:
        return None
    if resolved.is_file():
        parent = resolved.parent
        if parent.name == "bin":
            return parent.parent
        return parent
    return resolved


def primary_git_dir(worktree: Path) -> Path | None:
    """The primary checkout's git directory for a linked worktree.

    A linked worktree's ``.git`` is a ``gitdir: <primary>/.git/worktrees/<n>``
    pointer file; the probe sandbox must bind that primary git directory
    read-only because the worktree bind alone leaves it untouched.
    """
    git_path = worktree / ".git"
    try:
        if not git_path.is_file():
            return None
        content = git_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    # The pointer file format is exactly ``gitdir: <path>``.
    prefix, _, target = content.partition(":")
    if prefix != "gitdir" or not target.strip():
        return None
    try:
        gitdir = Path(target.strip()).expanduser().resolve()
    except OSError:
        return None
    if len(gitdir.parts) >= 2 and gitdir.parts[-2] == "worktrees":
        return Path(*gitdir.parts[:-2])
    return gitdir


def namespace_command(
    command: Sequence[str],
    *,
    worktree: Path,
    scratch: Path,
    scratch_bytes: int,
    readonly_binds: tuple[Sequence[Path], Sequence[Path]],
    label: str,
    working_directory: Path | None = None,
) -> list[str]:
    """Build the unshare launch for a probe.

    The probe runs in new user, mount, network, and pid namespaces. Strict
    read-only binds of the specific audit-state roots plus best-effort
    read-only binds of their mount points make the host filesystem read-only
    for the probe, a size-bounded tmpfs over scratch caps the total bytes the
    child can persist under scratch, and a keeper shell keeps the probe from
    becoming PID 1 of the pid namespace.
    """
    specific, mountpoints = readonly_binds
    working_directory = worktree if working_directory is None else working_directory
    resolved_working_directory = working_directory.resolve()
    if resolved_working_directory not in {worktree.resolve(), scratch.resolve()}:
        raise ValueError("sandbox working directory must be the worktree or scratch directory")
    if scratch_bytes <= 0:
        raise ValueError("scratch size bound must be positive")
    # --fork is required: with --pid alone the unshare process itself becomes
    # the namespace init and the kernel kills it on this util-linux/kernel
    # line; with --fork the forked child is the init (the keeper shell) and
    # the probe runs as an ordinary namespaced process.
    return [
        "/usr/bin/unshare",
        "--user",
        "--map-root-user",
        "--mount",
        "--net",
        "--pid",
        "--fork",
        "/bin/sh",
        "-c",
        _NAMESPACE_SCRIPT,
        label,
        str(worktree),
        str(scratch),
        str(scratch_bytes),
        str(resolved_working_directory),
        str(Path(__file__).resolve()),
        str(len(specific)),
        *(str(root) for root in specific),
        str(len(mountpoints)),
        *(str(mount) for mount in mountpoints),
        *command,
    ]


def live_process_count() -> int:
    """The live host task count that bounds a root-mapped sandbox.

    A ``--map-root-user`` probe is charged by the kernel against the
    initial user namespace's uid-0 (root) process count plus a small
    in-namespace delta, so the binding live count is the host's root
    process count. The maximum of the root and our own UID counts is used
    so the bound stays a safe superset under either kernel accounting.
    """
    counts: dict[int, int] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with Path("/proc", entry, "status").open("r", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("Uid:"):
                        fields = line.split()
                        if len(fields) > 1:
                            uid = int(fields[1])
                            try:
                                tasks = sum(1 for _ in Path("/proc", entry, "task").iterdir())
                            except OSError:
                                tasks = 1
                            counts[uid] = counts.get(uid, 0) + tasks
                        break
        except (OSError, ValueError):
            continue
    return max(counts.get(0, 0), counts.get(os.getuid(), 0))


def nproc_bound() -> int:
    """Host-relative RLIMIT_NPROC: live task count + margin, floored.

    The margin covers the sandbox's own in-namespace processes and count
    drift between the bound computation and the fork; the floor keeps a
    quiet host at a sane spawn budget.
    """
    return max(live_process_count() + NPROC_MARGIN, NPROC_FLOOR)


def current_resource_bounds() -> tuple[int, int]:
    """The (RLIMIT_AS, RLIMIT_NPROC) policy applied to sandbox children.

    Computed once in the parent so the bounds recorded in the probe result
    equal the bounds the child actually receives.
    """
    return ADDRESS_SPACE_BYTES, nproc_bound()


def make_limits(
    cpu_seconds: int,
    fsize_bytes: int,
    nofile: int,
    address_space: int,
    nproc: int,
) -> Callable[[], None]:
    """Build the preexec rlimit setter for a sandbox child."""

    def limits() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
        resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
        os.nice(_NICE_ADJUSTMENT)

    return limits


def restrict_writes_to(root: Path) -> None:
    """Use Landlock to deny filesystem mutations outside ``root``."""
    libc = ctypes.CDLL(None, use_errno=True)
    ruleset_attr = _LandlockRulesetAttr(_LANDLOCK_WRITE_ACCESS)
    ruleset_fd = libc.syscall(
        _LANDLOCK_CREATE_RULESET,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        0,
    )
    if ruleset_fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    path_fds: list[int] = []
    try:
        for path, access in ((root, _LANDLOCK_WRITE_ACCESS), (Path("/dev/null"), 1 << 1)):
            path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            path_fds.append(path_fd)
            path_attr = _LandlockPathBeneathAttr(access, path_fd)
            if (
                libc.syscall(
                    _LANDLOCK_ADD_RULE,
                    ruleset_fd,
                    _LANDLOCK_RULE_PATH_BENEATH,
                    ctypes.byref(path_attr),
                    0,
                )
                < 0
            ):
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        if libc.syscall(_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    finally:
        for path_fd in path_fds:
            os.close(path_fd)
        os.close(ruleset_fd)


def _main() -> int:
    if len(sys.argv) < 4 or sys.argv[1] != "--restrict-writes":
        raise SystemExit("usage: audit_sandbox.py --restrict-writes ROOT COMMAND [ARG ...]")
    restrict_writes_to(Path(sys.argv[2]))
    # The command was constructed from validated probe inputs by the parent;
    # exec is required so the Landlock policy applies to the actual probe.
    os.execvpe(sys.argv[3], sys.argv[3:], os.environ)  # noqa: S606
    return 127


if __name__ == "__main__":
    raise SystemExit(_main())


def kill_process_group(process: subprocess.Popen, signum: int) -> None:
    """Signal the child's process group.

    ESRCH (the group already exited) is normal timeout completion, not an
    error: without the guard the kill path would raise and the result
    artifact would never be written.
    """
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def wait_bounded(
    process: subprocess.Popen,
    wall_seconds: float,
    grace_seconds: float = KILL_GRACE_SECONDS,
) -> tuple[int, bool]:
    """Wait for the child, escalating to a group kill on wall timeout.

    The SIGKILL escalation is unconditional: it runs after the grace wait
    times out and also when the child exits early in the grace window,
    because namespace-init death does not mass-kill descendants and
    SIGTERM-ignoring descendants would otherwise survive. Returns
    ``(returncode, timed_out)``.
    """
    try:
        return process.wait(timeout=wall_seconds), False
    except subprocess.TimeoutExpired:
        kill_process_group(process, signal.SIGTERM)
        try:
            returncode = process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            kill_process_group(process, signal.SIGKILL)
            returncode = process.wait()
        else:
            kill_process_group(process, signal.SIGKILL)
        return returncode, True


def read_bounded(path: Path, limit: int) -> tuple[str, bool]:
    """Decode at most ``limit`` bytes of ``path`` and report truncation.

    Reading only ``limit + 1`` bytes keeps the limit a real parent-process
    memory bound: the rest of the file stays on disk (or in the child's
    address space) and is never copied into the parent.
    """
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    truncated = len(data) > limit
    return data[:limit].decode("utf-8", errors="replace"), truncated
