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

    def test_omitted_existing_title_is_stable_but_explicit_rename_invalidates(self) -> None:
        definition = {
            "area": "area/core",
            "title": "Custom Core",
            "description": "Core behavior.",
            "paths": ["src/core"],
            "entrypoints": ["main"],
            "boundaries": ["cli"],
        }
        self.areas.write_text(json.dumps({"areas": [definition]}), encoding="utf-8")
        self.call("reconcile", "--areas", str(self.areas), "--repo-sha", "sha1")
        document = self.project_dir / "workflows/gh-audit-repo/knowledge/areas/core.md"
        original = document.read_text(encoding="utf-8")
        original_marker = json.loads(original.split("\n-->", 1)[0].split("\n", 1)[1])

        definition.pop("title")
        self.areas.write_text(json.dumps({"areas": [definition]}), encoding="utf-8")
        unchanged = json.loads(
            self.call("reconcile", "--areas", str(self.areas), "--repo-sha", "sha2").stdout
        )
        assert unchanged == {"created": [], "invalidated": [], "unchanged": ["area/core"]}
        assert document.read_text(encoding="utf-8") == original

        definition["title"] = "Renamed Core"
        self.areas.write_text(json.dumps({"areas": [definition]}), encoding="utf-8")
        renamed = json.loads(
            self.call("reconcile", "--areas", str(self.areas), "--repo-sha", "sha3").stdout
        )
        assert renamed["invalidated"] == ["area/core"]
        replacement = json.loads(
            document.read_text(encoding="utf-8").split("\n-->", 1)[0].split("\n", 1)[1]
        )
        assert replacement["area"]["title"] == "Renamed Core"
        assert replacement["area"]["fingerprint"] != original_marker["area"]["fingerprint"]

    def test_repeated_invalidation_preserves_earlier_archives(self) -> None:
        def add_finding(payload_name: str, title: str, path: str) -> None:
            payload = self.root / payload_name
            payload.write_text(
                json.dumps(
                    {
                        "findings": [
                            {
                                "title": title,
                                "question": f"{title} question?",
                                "kind": "code",
                                "method": "inspection",
                                "observed_result": "reachable",
                                "conclusion": "yes",
                                "disposition": "confirmed",
                                "evidence_paths": [path],
                            }
                        ]
                    }
                )
            )
            self.call(
                "update",
                "--area",
                "area/core",
                "--input",
                str(payload),
                "--repo-sha",
                "current",
                "--expected-revision",
                "1",
            )

        def archive_documents() -> dict[str, dict]:
            store = self.project_dir / "workflows/gh-audit-repo/knowledge/invalidated"
            documents = {}
            for path in sorted(store.glob("*.md")):
                marker = json.loads(path.read_text().split("\n-->", 1)[0].split("\n", 1)[1])
                documents[path.name] = marker
            return documents

        # Definition A (paths src/core) is active and gains a finding.
        self.call("reconcile", "--areas", str(self.areas), "--repo-sha", "sha1")
        add_finding("first.json", "First lifetime", "src/core")
        # Flap to definition B: A is invalidated and archived.
        self.write_areas("src/flap")
        self.call("reconcile", "--areas", str(self.areas), "--repo-sha", "sha2")
        add_finding("flap.json", "Flap lifetime", "src/flap")
        # Restore definition A: B is invalidated and archived.
        self.write_areas("src/core")
        self.call("reconcile", "--areas", str(self.areas), "--repo-sha", "sha3")
        # Flap back to B: definition A is invalidated again and must not
        # clobber its first archive.
        self.write_areas("src/flap")
        result = json.loads(
            self.call("reconcile", "--areas", str(self.areas), "--repo-sha", "sha4").stdout
        )
        assert result["invalidated"] == ["area/core"]
        archives = archive_documents()
        assert len(archives) == 3
        by_fingerprint: dict[str, list[str]] = {}
        for name, marker in archives.items():
            by_fingerprint.setdefault(marker["area"]["fingerprint"], []).append(name)
        assert len(by_fingerprint) == 2
        for fingerprint, names in by_fingerprint.items():
            base = f"core-{fingerprint[:12]}"
            expected = [f"{base}.md"] + [f"{base}-{index}.md" for index in range(2, len(names) + 1)]
            assert sorted(names) == sorted(expected)
        surviving = [
            finding["title"]
            for marker in archives.values()
            for finding in marker.get("findings", [])
        ]
        assert sorted(surviving) == ["First lifetime", "Flap lifetime"]
        original = next(
            marker
            for marker in archives.values()
            if any(finding["title"] == "First lifetime" for finding in marker.get("findings", []))
        )
        assert original["revision"] == 2
        assert original["status"] == "invalidated"
        assert [finding["title"] for finding in original["findings"]] == ["First lifetime"]
        second = next(
            marker
            for marker in archives.values()
            if marker["area"]["fingerprint"] == original["area"]["fingerprint"]
            and marker is not original
        )
        assert second["revision"] == 1
        assert second["findings"] == []
        listed = json.loads(self.call("show").stdout)
        assert listed["invalidated"] == ["area/core"]
        assert listed["active"] == [{"area": "area/core", "findings": 0, "revision": 1}]

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

    def test_missing_or_non_string_area_identifier_is_rejected(self) -> None:
        for areas in (
            [{}],
            [{"area": 5, "description": "Core behavior.", "paths": ["src/core"]}],
        ):
            self.areas.write_text(json.dumps({"areas": areas}))
            failed = self.call(
                "reconcile", "--areas", str(self.areas), "--repo-sha", "sha1", check=False
            )
            assert failed.returncode == 2
