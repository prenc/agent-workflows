from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

import pytest

HELPER = Path(__file__).parents[1] / "src/github_workflows/audit_probe.py"


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

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
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
                "probe-1",
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
        assert result.returncode == 0, result.stdout + result.stderr
        summary = json.loads(result.stdout)
        artifact = json.loads(Path(summary["result"]).read_text())
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
