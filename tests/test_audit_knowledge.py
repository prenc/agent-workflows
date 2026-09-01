from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

HELPER = Path(__file__).parents[1] / "src/github_workflows/audit_knowledge.py"


class TestAuditKnowledge:
    def setup_method(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_dir = self.root / "project-state"
        self.areas = self.root / "areas.json"
        self.write_areas("src/core")

    def teardown_method(self) -> None:
        self.temp.cleanup()

    def write_areas(self, path: str) -> None:
        self.areas.write_text(
            json.dumps(
                {
                    "areas": [
                        {
                            "area": "area/core",
                            "description": "Core behavior.",
                            "paths": [path],
                            "entrypoints": ["main"],
                            "boundaries": ["cli"],
                        }
                    ]
                }
            )
        )

    def call(self, command: str, *args: str, check: bool = True):
        return subprocess.run(
            [str(HELPER), command, "--project-dir", str(self.project_dir), *args],
            check=check,
            capture_output=True,
            text=True,
        )

    def test_reconcile_update_and_selective_reuse(self) -> None:
        created = json.loads(
            self.call("reconcile", "--areas", str(self.areas), "--repo-sha", "sha1").stdout
        )
        assert created["created"][0]["area"] == "area/core"
        document = self.project_dir / "workflows/gh-audit-repo/knowledge/areas/core.md"
        assert "# Core" in document.read_text()
        marker = json.loads(document.read_text().split("\n-->", 1)[0].split("\n", 1)[1])
        assert marker["area"]["fingerprint"]
        update = self.root / "update.json"
        update.write_text(
            json.dumps(
                {
                    "findings": [
                        {
                            "title": "Code proof",
                            "question": "Does code work?",
                            "kind": "code",
                            "method": "inspection",
                            "observed_result": "reachable",
                            "conclusion": "yes",
                            "disposition": "confirmed",
                            "evidence_paths": ["src/core"],
                        },
                        {
                            "title": "API rule",
                            "question": "Is API supported?",
                            "kind": "documentation",
                            "method": "official docs",
                            "observed_result": "documented",
                            "conclusion": "supported",
                            "disposition": "confirmed",
                            "dependencies": {"tool": "1.2"},
                        },
                    ]
                }
            )
        )
        self.call(
            "update",
            "--area",
            "area/core",
            "--input",
            str(update),
            "--repo-sha",
            "sha1",
            "--expected-revision",
            "1",
        )
        versions = self.root / "versions.json"
        versions.write_text('{"tool":"1.2"}')
        context = json.loads(
            self.call("context", "--area", "area/core", "--versions", str(versions)).stdout
        )
        reuse = {item["title"]: item["reuse"] for item in context["findings"]}
        assert reuse == {"API rule": "reusable", "Code proof": "recheck"}
        listed = json.loads(self.call("show").stdout)
        assert listed["active"] == [{"area": "area/core", "findings": 2, "revision": 2}]
        shown = json.loads(self.call("show", "--area", "area/core").stdout)
        assert shown["area"]["id"] == "area/core"
        assert len(shown["findings"]) == 2

    def test_boundary_change_archives_and_bootstraps(self) -> None:
        self.call("reconcile", "--areas", str(self.areas), "--repo-sha", "sha1")
        self.write_areas("src/new-core")
        result = json.loads(
            self.call("reconcile", "--areas", str(self.areas), "--repo-sha", "sha2").stdout
        )
        assert result["invalidated"] == ["area/core"]
        assert list(
            (self.project_dir / "workflows/gh-audit-repo/knowledge/invalidated").glob("*.md")
        )

    def test_inconclusive_finding_is_rejected(self) -> None:
        self.call("reconcile", "--areas", str(self.areas), "--repo-sha", "sha1")
        update = self.root / "bad.json"
        update.write_text(
            json.dumps(
                {
                    "findings": [
                        {
                            "title": "Bad",
                            "question": "Q",
                            "kind": "code",
                            "method": "probe",
                            "observed_result": "failed",
                            "conclusion": "unknown",
                            "disposition": "inconclusive",
                        }
                    ]
                }
            )
        )
        failed = self.call(
            "update",
            "--area",
            "area/core",
            "--input",
            str(update),
            "--repo-sha",
            "sha1",
            "--expected-revision",
            "1",
            check=False,
        )
        assert failed.returncode == 2
