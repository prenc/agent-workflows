from __future__ import annotations

import json
import os
import subprocess
import tempfile
import venv
from pathlib import Path

HELPER = Path(__file__).parents[1] / "src/github_workflows/audit_inventory.py"


class TestAuditInventory:
    def setup_method(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="audit-inventory-test-")
        self.project = Path(self.temporary.name).resolve()
        self.project_dir = self.project / "qwen-project"
        self.run_dir = self.project_dir / "workflows" / "gh-audit-repo" / "current"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "state.json").write_text(
            '{"schema_version":1,"status":"in-progress","revision":1}\n'
        )
        (self.run_dir / "journal.jsonl").write_text("")
        self.worktree = self.project / "worktree"
        self.worktree.mkdir()

    def teardown_method(self) -> None:
        self.temporary.cleanup()

    def call(
        self, command: str, *arguments: str, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(HELPER),
                command,
                "--project-root",
                str(self.project),
                "--audit-worktree",
                str(self.worktree),
                "--run-dir",
                str(self.run_dir),
                "--project-dir",
                str(self.project_dir),
                *arguments,
            ],
            check=check,
            capture_output=True,
            text=True,
            env=env,
        )

    def program_input(self, programs: list[dict[str, object]]) -> Path:
        path = self.project / "programs.json"
        path.write_text(json.dumps(programs))
        return path

    def test_initialize_falls_back_to_system_python(self) -> None:
        result = json.loads(self.call("initialize").stdout)
        assert result["revision"] == 1
        assert result["state_revision"] == 2
        inventory = json.loads((self.run_dir / "state.json").read_text())["inventory"]
        python = inventory["sources"]["python_environment"]
        assert python["available"]
        assert python["source"] == "system"
        assert inventory["schema_version"] == 1
        assert inventory["revision"] == 1
        event = json.loads((self.run_dir / "journal.jsonl").read_text().splitlines()[0])
        assert event["event"] == "inventory_initialized"
        assert event["state_revision"] == 2

    def test_program_probe_updates_shared_inventory(self) -> None:
        self.call("initialize")
        source = self.program_input(
            [{"name": "git", "arguments": ["--version"]}, {"name": "missing-fixture"}]
        )
        result = self.call("program", "--input", str(source), "--expected-revision", "1")
        payload = json.loads(result.stdout)
        assert payload["revision"] == 2
        assert payload["state_revision"] == 3
        assert payload["facts"]["git"]["available"]
        assert payload["facts"]["git"]["probe_status"] == "succeeded"
        assert "git version" in payload["facts"]["git"]["stdout"]
        assert payload["facts"]["missing-fixture"]["probe_status"] == "not-found"

    def test_initialize_uses_existing_project_venv_and_its_packages(self) -> None:
        venv.EnvBuilder(with_pip=False).create(self.project / ".venv")
        site_packages = next((self.project / ".venv" / "lib").glob("python*/site-packages"))
        metadata = site_packages / "audit_fixture-1.2.3.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: audit-fixture\nVersion: 1.2.3\n"
        )
        self.call("initialize")
        inventory = json.loads((self.run_dir / "state.json").read_text())["inventory"]
        python = inventory["sources"]["python_environment"]
        assert python["available"]
        assert python["source"] == "project-venv"
        assert python["python"]
        assert python["packages"]["audit-fixture"] == "1.2.3"

    def test_revision_conflict_does_not_replace_inventory(self) -> None:
        self.call("initialize")
        source = self.program_input([{"name": "git", "arguments": ["--version"]}])
        failed = self.call(
            "program", "--input", str(source), "--expected-revision", "0", check=False
        )
        assert failed.returncode == 2
        inventory = json.loads((self.run_dir / "state.json").read_text())["inventory"]
        assert inventory["revision"] == 1

    def test_refresh_and_context_fact_advance_shared_revision(self) -> None:
        self.call("initialize")
        refreshed = json.loads(self.call("refresh", "--expected-revision", "1").stdout)
        assert refreshed["revision"] == 2
        assert not refreshed["changed"]
        fact = self.project / "fact.json"
        fact.write_text('{"kind":"capability","available":true}')
        recorded = json.loads(
            self.call(
                "record-context",
                "--request-id",
                "tmux-popup",
                "--input",
                str(fact),
                "--expected-revision",
                "2",
            ).stdout
        )
        assert recorded["revision"] == 3
        inventory = json.loads((self.run_dir / "state.json").read_text())["inventory"]
        assert inventory["sources"]["context"]["tmux-popup"]["available"]

    def test_program_probe_has_read_only_worktree_and_no_network(self) -> None:
        self.call("initialize")
        with tempfile.TemporaryDirectory(prefix="audit-inventory-bin-") as binary_dir:
            executable = Path(binary_dir) / "inventory-fixture"
            executable.write_text(
                "#!/usr/bin/python3\n"
                "from pathlib import Path\n"
                "import socket\n"
                "try:\n Path('forbidden-write').write_text('x')\n"
                "except OSError:\n pass\n"
                "else:\n raise SystemExit(8)\n"
                "try:\n socket.create_connection(('1.1.1.1', 53), timeout=0.1)\n"
                "except OSError:\n pass\n"
                "else:\n raise SystemExit(9)\n"
                "print('isolated version')\n"
            )
            executable.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{binary_dir}:{environment['PATH']}"
            source = self.program_input([{"name": "inventory-fixture", "arguments": ["--version"]}])
            result = self.call(
                "program", "--input", str(source), "--expected-revision", "1", env=environment
            )
        fact = json.loads(result.stdout)["facts"]["inventory-fixture"]
        assert fact["available"]
        assert "isolated version" in fact["stdout"]
        assert not (self.worktree / "forbidden-write").exists()

    def test_failed_version_probe_keeps_executable_available(self) -> None:
        self.call("initialize")
        with tempfile.TemporaryDirectory(prefix="audit-inventory-bin-") as binary_dir:
            executable = Path(binary_dir) / "versionless"
            executable.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            executable.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{binary_dir}:{environment['PATH']}"
            source = self.program_input([{"name": "versionless", "arguments": ["--version"]}])
            result = self.call(
                "program", "--input", str(source), "--expected-revision", "1", env=environment
            )
        fact = json.loads(result.stdout)["facts"]["versionless"]
        assert fact["available"]
        assert fact["probe_status"] == "failed"
        assert fact["returncode"] == 7
