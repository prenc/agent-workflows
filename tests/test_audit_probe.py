from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import venv
from pathlib import Path
from unittest import mock

import pytest

from github_workflows import audit_probe, audit_sandbox

HELPER = Path(__file__).parents[1] / "src/github_workflows/audit_probe.py"


def _pids_with_marker(marker: str) -> list[int]:
    """Host pid-space pids whose cmdline still carries the marker."""
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmdline = Path("/proc", entry, "cmdline").read_bytes()
        except OSError:
            continue
        if marker.encode("utf-8") in cmdline:
            pids.append(int(entry))
    return pids


class TestAuditProbe:
    def setup_method(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="audit-probe-test-")
        self.project = Path(self.temporary.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(
            ["git", "-C", str(self.project), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.project), "config", "user.name", "Audit Probe Test"],
            check=True,
        )
        (self.project / "tracked.txt").write_text("unchanged\n", encoding="utf-8")
        (self.project / ".gitignore").write_text(
            ".worktrees/\n.venv\nqwen-project/\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.project), "add", "tracked.txt", ".gitignore"],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.project), "commit", "-qm", "fixture"], check=True)
        self.project_dir = self.project / "qwen-project"
        self.run_dir = self.project_dir / "workflows" / "gh-audit-repo" / "current"
        self.run_dir.mkdir(parents=True)

    def teardown_method(self) -> None:
        self.temporary.cleanup()

    def invoke(
        self, *arguments: str, probe_id: str = "probe-1"
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(HELPER),
                "python",
                "--project-root",
                str(self.project),
                "--audit-worktree",
                str(self.project),
                "--run-dir",
                str(self.run_dir),
                "--project-dir",
                str(self.project_dir),
                "--probe-id",
                probe_id,
                *arguments,
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_python_probe_has_read_only_worktree_and_no_network(self) -> None:
        code = """
from pathlib import Path
import socket

try:
    Path("tracked.txt").write_text("changed")
except OSError:
    pass
else:
    raise AssertionError("audit worktree was writable")

try:
    socket.create_connection(("1.1.1.1", 53), timeout=0.1)
except OSError:
    pass
else:
    raise AssertionError("network was reachable")

print("isolated")
"""
        result = self.invoke("--code", code)
        summary = json.loads(result.stdout)
        artifact = json.loads(Path(summary["result"]).read_text())
        assert result.returncode == 0, result.stdout + result.stderr + artifact["stderr_excerpt"]
        assert artifact["schema_version"] == 1
        assert artifact["probe_status"] == "succeeded"
        assert artifact["environment"]["python_source"] == "system"
        assert artifact["probe"]["code"] == code
        assert "code_sha256" in artifact["probe"]
        assert artifact["worktree_unchanged"]
        assert not artifact["timed_out"]
        assert "isolated" in artifact["stdout_excerpt"]
        assert (self.project / "tracked.txt").read_text() == "unchanged\n"

    def test_script_interface_is_removed(self) -> None:
        outside = self.project / "outside.py"
        outside.write_text("print('no')\n", encoding="utf-8")
        result = subprocess.run(
            [
                str(HELPER),
                "python",
                "--project-root",
                str(self.project),
                "--audit-worktree",
                str(self.project),
                "--run-dir",
                str(self.run_dir),
                "--project-dir",
                str(self.project_dir),
                "--probe-id",
                "probe-2",
                "--code",
                "print('visible')",
                "--script",
                str(outside),
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert result.returncode == 2
        assert "unrecognized arguments: --script" in result.stderr

    def test_rejects_oversized_inline_code(self) -> None:
        result = self.invoke("--code", "x" * (8 * 1024 + 1))
        assert result.returncode == 2
        assert "inline Python" in result.stderr
        # A refused attempt must not pre-create artifacts that poison the id.
        assert not (self.run_dir / "validation" / "probe-1").exists()
        retry = self.invoke("--code", "print('retry')")
        assert retry.returncode == 0, retry.stdout + retry.stderr

    def test_pythonpath_root_is_importable_and_fingerprinted(self) -> None:
        source_root = self.project / "source"
        source_root.mkdir()
        (source_root / "fixture_root.py").write_text(
            "print('from worktree source root')\n", encoding="utf-8"
        )
        result = self.invoke("--pythonpath", "source", "--code", "import fixture_root")
        assert result.returncode == 0, result.stdout + result.stderr
        summary = json.loads(result.stdout)
        assert summary["probe_status"] == "succeeded"
        artifact = json.loads(Path(summary["result"]).read_text())
        assert artifact["environment"]["pythonpath"] == str(source_root.resolve())
        assert "from worktree source root" in artifact["stdout_excerpt"]

        bare = self.invoke("--code", "print('ok')", probe_id="probe-nop")
        assert bare.returncode == 0, bare.stdout + bare.stderr
        bare_artifact = json.loads(Path(json.loads(bare.stdout)["result"]).read_text())
        assert bare_artifact["environment"]["pythonpath"] is None

    @pytest.mark.parametrize("program_exit", [3, 124])
    def test_exit_code_is_the_programs_own_code(self, program_exit: int) -> None:
        probe_id = f"probe-exit-{program_exit}"
        result = self.invoke("--code", f"import sys; sys.exit({program_exit})", probe_id=probe_id)
        assert result.returncode == program_exit, result.stdout + result.stderr
        summary = json.loads(result.stdout)
        assert summary["probe_status"] == "failed"
        artifact = json.loads(Path(summary["result"]).read_text())
        assert artifact["probe_status"] == "failed"
        assert artifact["returncode"] == program_exit
        assert artifact["worktree_unchanged"] is True
        assert artifact["timed_out"] is False

    def test_python_probe_uses_project_venv_site_packages(self) -> None:
        venv.EnvBuilder(with_pip=False).create(self.project / ".venv")
        site_packages = next((self.project / ".venv" / "lib").glob("python*/site-packages"))
        (site_packages / "audit_fixture.py").write_text("VALUE = 'from-project-venv'\n")
        result = self.invoke(
            "--code",
            "import audit_fixture; print(audit_fixture.VALUE)",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        artifact = json.loads(Path(json.loads(result.stdout)["result"]).read_text())
        assert artifact["environment"]["python_source"] == "project-venv"
        assert "from-project-venv" in artifact["stdout_excerpt"]

    def test_probe_id_cannot_overwrite_an_existing_attempt(self) -> None:
        first = self.invoke("--code", "print('first')")
        assert first.returncode == 0, first.stdout + first.stderr
        second = self.invoke("--code", "print('second')")
        assert second.returncode == 2
        assert "unique id" in second.stderr

    def test_focused_pytest_selector(self) -> None:
        if importlib.util.find_spec("pytest") is None:
            pytest.skip("pytest is unavailable in the system interpreter")
        test_file = self.project / "test_sample.py"
        test_file.write_text(
            "def test_selected():\n    assert 2 + 2 == 4\n",
            encoding="utf-8",
        )
        (self.project / ".venv").symlink_to(
            Path(sys.executable).parents[1], target_is_directory=True
        )
        subprocess.run(
            ["git", "-C", str(self.project), "add", "test_sample.py"],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.project), "commit", "-qm", "add test"], check=True)
        result = subprocess.run(
            [
                str(HELPER),
                "pytest",
                "--project-root",
                str(self.project),
                "--audit-worktree",
                str(self.project),
                "--run-dir",
                str(self.run_dir),
                "--project-dir",
                str(self.project_dir),
                "--probe-id",
                "probe-pytest",
                "--pythonpath",
                ".",
                "--selector",
                "test_sample.py::test_selected",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert result.stdout, result.stderr
        summary = json.loads(result.stdout)
        artifact = json.loads(Path(summary["result"]).read_text())
        assert "stdout_excerpt" in artifact, artifact
        assert result.returncode == 0, artifact["stdout_excerpt"] + artifact["stderr_excerpt"]
        assert "1 passed" in artifact["stdout_excerpt"]

    def test_missing_pytest_is_a_nonfatal_coverage_limitation(self) -> None:
        venv_bin = self.project / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        python = venv_bin / "python"
        python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        python.chmod(0o755)
        test_file = self.project / "test_missing.py"
        test_file.write_text("def test_missing():\n    pass\n", encoding="utf-8")
        result = subprocess.run(
            [
                str(HELPER),
                "pytest",
                "--project-root",
                str(self.project),
                "--audit-worktree",
                str(self.project),
                "--run-dir",
                str(self.run_dir),
                "--project-dir",
                str(self.project_dir),
                "--probe-id",
                "probe-no-pytest",
                "--selector",
                "test_missing.py::test_missing",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert result.returncode == 0, result.stderr
        summary = json.loads(result.stdout)
        artifact = json.loads(Path(summary["result"]).read_text())
        assert artifact["probe_status"] == "unavailable"
        assert artifact["environment"]["python_source"] == "project-venv"

    def test_git_output_passes_explicit_timeout(self) -> None:
        completed = subprocess.CompletedProcess(["git"], 0, stdout="sha\n", stderr="")
        with mock.patch(
            "github_workflows.audit_probe.subprocess.run", return_value=completed
        ) as run:
            assert audit_probe.git_output(Path("/missing"), "rev-parse", "HEAD") == "sha"
        assert run.call_args.kwargs["timeout"] == audit_probe.GIT_SECONDS

    def test_git_output_timeout_is_a_bounded_error(self) -> None:
        with mock.patch(
            "github_workflows.audit_probe.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=audit_probe.GIT_SECONDS),
        ):
            with pytest.raises(ValueError, match="timed out after"):
                audit_probe.git_output(Path("/missing"), "status")

    def test_sandbox_enforces_and_records_as_and_nproc_limits(self) -> None:
        code = (
            "import json, resource\n"
            "print(json.dumps({'as': list(resource.getrlimit(resource.RLIMIT_AS)), "
            "'nproc': list(resource.getrlimit(resource.RLIMIT_NPROC))}))\n"
        )
        result = self.invoke("--code", code)
        assert result.returncode == 0, result.stdout + result.stderr
        artifact = json.loads(Path(json.loads(result.stdout)["result"]).read_text())
        assert artifact["probe_status"] == "succeeded"
        observed = json.loads(artifact["stdout_excerpt"].strip())
        assert observed["as"] == [audit_sandbox.ADDRESS_SPACE_BYTES] * 2
        assert observed["nproc"] == [artifact["limits"]["nproc"]] * 2
        assert artifact["limits"]["address_space_bytes"] == audit_sandbox.ADDRESS_SPACE_BYTES
        assert artifact["limits"]["address_space_bytes"] >= 1024 * 1024 * 1024
        assert artifact["limits"]["nproc"] >= audit_sandbox.NPROC_FLOOR
        assert artifact["limits"]["scratch_bytes"] == audit_sandbox.SCRATCH_BYTES

    def test_address_space_exhaustion_maps_to_failed_probe(self) -> None:
        # exhausting the RLIMIT_AS ceiling is a child MemoryError: it exits
        # non-zero and maps to probe_status failed with no new exception path
        code = "chunks = []\nwhile True:\n    chunks.append(bytearray(64 * 1024 * 1024))\n"
        result = self.invoke("--code", code)
        assert result.returncode == 1, result.stdout + result.stderr
        artifact = json.loads(Path(json.loads(result.stdout)["result"]).read_text())
        assert artifact["probe_status"] == "failed"
        assert artifact["returncode"] == 1

    def test_scratch_fill_bounded_by_tmpfs_maps_to_failed_probe(self, monkeypatch) -> None:
        # Filling the size-bounded scratch tmpfs is a child ENOSPC: the probe
        # fails cleanly at the configured total instead of exhausting the host
        # volume, with no new exception path (exact precedent: address-space
        # exhaustion).
        from github_workflows import audit_probe as audit_probe_module

        bound = 512 * 1024
        monkeypatch.setattr(audit_probe_module.audit_sandbox, "SCRATCH_BYTES", bound)
        code = (
            "import os\n"
            "total = 0\n"
            "tmp = os.environ['TMPDIR']\n"
            "try:\n"
            "    i = 0\n"
            "    while True:\n"
            "        with open(os.path.join(tmp, f'fill-{i}'), 'wb') as handle:\n"
            "            handle.write(b'x' * (64 * 1024))\n"
            "        total += 64 * 1024\n"
            "        i += 1\n"
            "except OSError:\n"
            "    print(f'scratch bounded at {total} bytes', flush=True)\n"
            "    raise\n"
        )
        args = argparse.Namespace(
            project_root=self.project,
            project_dir=self.project_dir,
            audit_worktree=self.project,
            run_dir=self.run_dir,
            probe_id="probe-scratch-bound",
            pythonpath=None,
            kind="python",
            code=code,
            selector=[],
        )
        returncode = audit_probe_module.run_probe(args)
        assert returncode == 1, "scratch exhaustion must surface as the child's non-zero exit"
        artifact = json.loads(
            (self.run_dir / "validation" / "probe-scratch-bound" / "result.json").read_text()
        )
        assert artifact["probe_status"] == "failed"
        assert artifact["returncode"] == 1
        assert artifact["timed_out"] is False
        assert artifact["worktree_unchanged"] is True
        assert artifact["limits"]["scratch_bytes"] == bound
        match = re.search(r"scratch bounded at (\d+) bytes", artifact["stdout_excerpt"])
        assert match, artifact["stdout_excerpt"] + artifact["stderr_excerpt"]
        # The tmpfs bound, not the host volume, is what stopped the child: the
        # reported total holds at or below the configured scratch ceiling.
        assert 0 < int(match.group(1)) <= bound
        assert "No space left on device" in artifact["stderr_excerpt"]

    def test_sandbox_isolates_pid_namespace_and_primary_gitdir(self) -> None:
        linked = self.project / "linked-worktree"
        subprocess.run(
            ["git", "-C", str(self.project), "worktree", "add", "--detach", "-q", str(linked)],
            check=True,
        )
        try:
            primary_gitdir = (self.project / ".git").resolve()
            code = f"""
from pathlib import Path

nspid = [line for line in Path("/proc/self/status").read_text().splitlines() if line.startswith("NSpid")]
assert len(nspid) == 1 and len(nspid[0].split()) >= 3, f"no nested pid namespace: {{nspid}}"

try:
    (Path({str(primary_gitdir)!r}) / "probe-write-test").open("w").write("x")
except OSError:
    pass
else:
    raise AssertionError("primary git directory was writable")

print("namespaced")
"""
            result = subprocess.run(
                [
                    str(HELPER),
                    "python",
                    "--project-root",
                    str(self.project),
                    "--audit-worktree",
                    str(linked),
                    "--run-dir",
                    str(self.run_dir),
                    "--project-dir",
                    str(self.project_dir),
                    "--probe-id",
                    "probe-ns",
                    "--code",
                    code,
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            artifact = json.loads(Path(json.loads(result.stdout)["result"]).read_text())
            assert result.returncode == 0, (
                result.stdout + result.stderr + artifact["stderr_excerpt"]
            )
            assert artifact["probe_status"] == "succeeded"
            assert "namespaced" in artifact["stdout_excerpt"]
            assert not (primary_gitdir / "probe-write-test").exists()
        finally:
            subprocess.run(
                ["git", "-C", str(self.project), "worktree", "remove", "--force", str(linked)],
                check=True,
            )

    def test_timeout_writes_result_and_kills_ignoring_probe(self, monkeypatch) -> None:
        from github_workflows import audit_probe as audit_probe_module

        monkeypatch.setattr(audit_probe_module, "WALL_SECONDS", 2)
        marker = f"qwen-poison-marker-{os.getpid()}-{id(self)}"
        code = (
            "import signal, time\n"
            f"# {marker}\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(987.654)\n"
        )
        args = argparse.Namespace(
            project_root=self.project,
            project_dir=self.project_dir,
            audit_worktree=self.project,
            run_dir=self.run_dir,
            probe_id="probe-timeout",
            pythonpath=None,
            kind="python",
            code=code,
            selector=[],
        )
        returncode = audit_probe_module.run_probe(args)
        assert returncode == 124
        # the result artifact must be written even though the group kill
        # raced a SIGTERM-ignoring probe: a skipped artifact would poison
        # the probe id for every retry
        artifact_path = self.run_dir / "validation" / "probe-timeout" / "result.json"
        assert artifact_path.is_file()
        artifact = json.loads(artifact_path.read_text())
        assert artifact["probe_status"] == "timed-out"
        assert artifact["timed_out"] is True
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pids_with_marker(marker):
            time.sleep(0.1)
        assert not _pids_with_marker(marker), "SIGTERM-ignoring probe survived the timeout"

    def test_sandbox_rejects_writes_outside_worktree_and_scratch(self) -> None:
        host_path = self.project.parent / "unrelated-host-write"
        code = f"""
from pathlib import Path

try:
    Path({str(host_path)!r}).write_text("x")
except OSError:
    pass
else:
    raise AssertionError("unrelated host path was writable")

Path.home().joinpath("scratch-write").write_text("ok")
print("isolated")
"""
        result = self.invoke("--code", code)
        assert result.returncode == 0, result.stdout + result.stderr
        artifact = json.loads(Path(json.loads(result.stdout)["result"]).read_text())
        assert artifact["probe_status"] == "succeeded"
        assert "isolated" in artifact["stdout_excerpt"]
        assert not host_path.exists()

    def test_sandbox_drops_capabilities_and_refuses_bind_mounts(self) -> None:
        host_path = self.project.parent / "unrelated-host-source"
        host_path.write_text("host data\n", encoding="utf-8")
        try:
            code = f"""
import ctypes, errno, os
from pathlib import Path

caps = dict(
    (line.split(":")[0].strip(), line.split(":")[1].strip())
    for line in Path("/proc/self/status").read_text().splitlines()
    if line.startswith(("CapEff", "CapPrm", "CapInh"))
)
if any(int(value, 16) for value in caps.values()):
    raise AssertionError("capability sets were not dropped: %r" % caps)

target = Path(os.environ["TMPDIR"]) / "bind-alias"
target.mkdir()
libc = ctypes.CDLL(None, use_errno=True)
# mount(2) with the raw MS_BIND flag 4096: os.MS_BIND is absent in CPython 3.12
rc = libc.mount(
    {str(host_path)!r}.encode(),
    str(target).encode(),
    None,
    4096,
    None,
)
if rc == 0:
    raise AssertionError("bind mount into scratch succeeded")
if ctypes.get_errno() not in (errno.EPERM, errno.EACCES):
    raise AssertionError("bind mount was not refused: %d" % ctypes.get_errno())
print("mounts-refused")
"""
            result = self.invoke("--code", code)
            assert result.returncode == 0, result.stdout + result.stderr
            artifact = json.loads(Path(json.loads(result.stdout)["result"]).read_text())
            assert artifact["probe_status"] == "succeeded"
            assert "mounts-refused" in artifact["stdout_excerpt"]
            assert host_path.read_text() == "host data\n"
        finally:
            host_path.unlink(missing_ok=True)


class TestAuditSandboxKills:
    def test_kill_process_group_ignores_missing_group(self) -> None:
        process = subprocess.Popen(["true"], start_new_session=True)
        process.wait()
        audit_sandbox.kill_process_group(process, signal.SIGTERM)
        audit_sandbox.kill_process_group(process, signal.SIGKILL)

    def test_wait_bounded_escalates_to_sigkill_for_term_ignoring_group(self) -> None:
        process = subprocess.Popen(
            ["/bin/sh", "-c", 'trap "" TERM; sleep 30 & wait $!'],
            start_new_session=True,
        )
        pgid = process.pid
        returncode, timed_out = audit_sandbox.wait_bounded(process, 0.5, grace_seconds=1)
        assert timed_out is True
        assert returncode != 0
        # the orphaned members are reaped by the host init shortly after the
        # SIGKILL; poll until the group is provably empty
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail("SIGTERM-ignoring group survived the SIGKILL escalation")

    def test_wait_bounded_tolerates_group_exit_during_grace(self) -> None:
        process = subprocess.Popen(["/bin/sh", "-c", "sleep 5"], start_new_session=True)
        returncode, timed_out = audit_sandbox.wait_bounded(process, 0.3, grace_seconds=2)
        assert timed_out is True
        assert returncode != 0

    def test_wait_bounded_gives_up_on_unreapable_child(self) -> None:
        # A stubbed Popen whose wait keeps timing out models a SIGKILLed
        # child stuck in uninterruptible D state (for example on a stalled
        # network-filesystem write): the bounded reap wait must return the
        # typed timeout outcome instead of wedging the caller.
        process = subprocess.Popen(["true"], start_new_session=True)
        process.wait()

        def always_times_out(timeout=None):
            raise subprocess.TimeoutExpired(cmd=["unreapable"], timeout=timeout)

        process.wait = always_times_out
        started = time.monotonic()
        returncode, timed_out = audit_sandbox.wait_bounded(
            process, 0.1, grace_seconds=0.1, reap_seconds=0.1
        )
        assert timed_out is True
        assert returncode is None
        assert time.monotonic() - started < 5
