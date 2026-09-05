#!/usr/bin/env python3
"""Manage per-area Markdown knowledge for Qwen repository audits."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

AREA_RE = re.compile(r"area/[a-z0-9][a-z0-9._-]{0,127}")
MARKER_START = "<!-- qwen-audit-kb:v1\n"
MARKER_END = "\n-->"
DISPOSITIONS = {"confirmed", "disproved"}
REUSABLE_KINDS = {"documentation", "capability"}


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def secure_file(path: Path) -> None:
    os.chmod(path, 0o600)


def project_dir(args: argparse.Namespace) -> Path:
    configured = args.project_dir or os.environ.get("QWEN_CODE_PROJECT_DIR")
    if not configured:
        raise ValueError(
            "QWEN_CODE_PROJECT_DIR is required outside tests; use --project-dir explicitly"
        )
    root = Path(configured).expanduser().resolve()
    secure_directory(root)
    return root


def knowledge_root(args: argparse.Namespace) -> Path:
    root = project_dir(args) / "workflows" / "gh-audit-repo" / "knowledge"
    secure_directory(root / "areas")
    secure_directory(root / "invalidated")
    return root


def slug(area: str) -> str:
    if not AREA_RE.fullmatch(area):
        raise ValueError("area must use area/<slug>")
    return area.removeprefix("area/")


def canonical_area(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("each area must be a JSON object")
    area = value.get("area") or value.get("id")
    if area is None and isinstance(value.get("title"), str) and value["title"].startswith("area/"):
        area = value["title"]
    if not isinstance(area, str) or not area:
        raise ValueError("each area requires a non-empty string area identifier")
    slug(area)
    title = value.get("title")
    if title == area:
        title = None
    if title is None:
        title = slug(area).replace("-", " ").replace("_", " ").capitalize()
    description = value.get("description")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"{area} requires a title")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"{area} requires a description")
    result = {"id": area, "title": title.strip(), "description": description.strip()}
    for key in ("paths", "entrypoints", "boundaries"):
        items = value.get(key, [])
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not item for item in items
        ):
            raise ValueError(f"{area} {key} must be a list of non-empty strings")
        result[key] = sorted(set(items))
    rendered = json.dumps(result, sort_keys=True, separators=(",", ":"))
    result["fingerprint"] = hashlib.sha256(rendered.encode()).hexdigest()
    return result


def load_areas(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    raw = value.get("areas") if isinstance(value, dict) else None
    if not isinstance(raw, list):
        raise ValueError("areas input must contain an areas list")
    areas = [canonical_area(item) for item in raw]
    ids = [item["id"] for item in areas]
    if len(ids) != len(set(ids)):
        raise ValueError("area IDs must be unique")
    owned: dict[str, str] = {}
    for area in areas:
        for path_name in area["paths"]:
            previous = owned.setdefault(path_name, area["id"])
            if previous != area["id"]:
                raise ValueError(f"path {path_name} belongs to both {previous} and {area['id']}")
    return areas


def parse_document(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith(MARKER_START):
        raise ValueError(f"knowledge file lacks the v1 marker: {path}")
    end = text.find(MARKER_END, len(MARKER_START))
    if end < 0:
        raise ValueError(f"knowledge file has an unterminated marker: {path}")
    value = json.loads(text[len(MARKER_START) : end])
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError(f"knowledge file has an incompatible schema: {path}")
    return value


def active_documents(root: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    result: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in sorted((root / "areas").glob("*.md")):
        value = parse_document(path)
        area = value.get("area", {}).get("id")
        slug(area)
        if area in result:
            raise ValueError(f"duplicate active knowledge for {area}")
        result[area] = (path, value)
    return result


def bullet(value: Any) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return "none"
    if isinstance(value, dict):
        return ", ".join(f"{key}={item}" for key, item in sorted(value.items()))
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def render_document(value: dict[str, Any]) -> str:
    metadata = json.dumps(value, indent=2, sort_keys=True)
    area = value["area"]
    lines = [
        f"{MARKER_START}{metadata}{MARKER_END}",
        "",
        f"# {area['title']}",
        "",
        "## What this area entails",
        "",
        area["description"],
        "",
        "### Owned paths",
        "",
    ]
    lines.extend(f"- `{path}`" for path in area["paths"] or ["none"])
    lines.extend(["", "### Entry points", ""])
    lines.extend(f"- {item}" for item in area["entrypoints"] or ["none"])
    lines.extend(["", "### Shared boundaries", ""])
    lines.extend(f"- {item}" for item in area["boundaries"] or ["none"])
    lines.extend(["", "## Findings", ""])
    findings = value.get("findings", [])
    if not findings:
        lines.append("No retained findings.")
    for finding in findings:
        lines.extend(
            [
                f"### {finding['title']}",
                "",
                f"- **Question:** {finding['question']}",
                f"- **Kind:** {finding['kind']}",
                f"- **Versions or constraints:** {bullet(finding.get('dependencies'))}",
                f"- **Evidence:** {bullet(finding.get('evidence_paths'))}",
                f"- **How obtained:** {finding['method']}",
                f"- **Observed result:** {finding['observed_result']}",
                f"- **Conclusion:** {finding['conclusion']}",
                f"- **Disposition:** {finding['disposition']}",
                f"- **Last validated SHA:** `{finding['validated_sha']}`",
                "",
            ]
        )
    lines.extend(["## Bootstrap leads", ""])
    leads = value.get("bootstrap_leads", [])
    if not leads:
        lines.append("No bootstrap leads.")
    for lead in leads:
        lines.extend(
            [
                f"### {lead['title']}",
                "",
                f"- **From area:** `{lead['source_area']}`",
                f"- **Question:** {lead['question']}",
                f"- **Prior conclusion:** {lead['conclusion']}",
                f"- **Evidence:** {bullet(lead.get('evidence_paths'))}",
                "- **Status:** requires revalidation",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_document(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".md.new")
    temporary.write_text(render_document(value), encoding="utf-8")
    secure_file(temporary)
    os.replace(temporary, path)
    secure_file(path)


def area_document(
    area: dict[str, Any], repo_sha: str, leads: list[dict[str, Any]]
) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": 1,
        "status": "active",
        "revision": 1,
        "area": area,
        "source_sha": repo_sha,
        "created_at": now,
        "updated_at": now,
        "findings": [],
        "bootstrap_leads": leads,
    }


def overlap(old: dict[str, Any], new: dict[str, Any]) -> set[str]:
    return set(old["area"]["paths"]).intersection(new["paths"])


def bootstrap(old_documents: list[dict[str, Any]], area: dict[str, Any]) -> list[dict[str, Any]]:
    leads: dict[tuple[str, str], dict[str, Any]] = {}
    new_paths = set(area["paths"])
    for old in old_documents:
        shared = overlap(old, area)
        if not shared:
            continue
        for finding in [*old.get("findings", []), *old.get("bootstrap_leads", [])]:
            evidence = set(finding.get("evidence_paths", []))
            if evidence and not evidence.intersection(new_paths):
                continue
            lead = dict(finding)
            lead["source_area"] = old["area"]["id"]
            lead.pop("reuse", None)
            leads[(lead.get("id", lead["title"]), lead["source_area"])] = lead
    return list(leads.values())


def plan_reconciliation(
    areas: list[dict[str, Any]], active: dict[str, tuple[Path, dict[str, Any]]]
) -> dict[str, Any]:
    current = {area["id"]: area for area in areas}
    unchanged, invalidated, created = [], [], []
    for area_id, (_, document) in active.items():
        area = current.get(area_id)
        if area and area["fingerprint"] == document["area"]["fingerprint"]:
            unchanged.append(area_id)
        else:
            invalidated.append(area_id)
    for area in areas:
        if area["id"] not in unchanged:
            sources = [old_id for old_id, (_, old) in active.items() if overlap(old, area)]
            created.append({"area": area["id"], "bootstrap_from": sorted(sources)})
    return {"unchanged": sorted(unchanged), "invalidated": sorted(invalidated), "created": created}


def status(args: argparse.Namespace) -> None:
    show(args)


def reconcile(args: argparse.Namespace) -> None:
    root = knowledge_root(args)
    areas = load_areas(args.areas)
    active = active_documents(root)
    plan = plan_reconciliation(areas, active)
    invalidated_documents: list[dict[str, Any]] = []
    for area_id in plan["invalidated"]:
        path, document = active[area_id]
        document["status"] = "invalidated"
        document["invalidated_at"] = utc_now()
        archive = (
            root / "invalidated" / f"{slug(area_id)}-{document['area']['fingerprint'][:12]}.md"
        )
        write_document(archive, document)
        path.unlink()
        invalidated_documents.append(document)
    available_sources = [document for _, document in active.values()]
    for area in areas:
        if area["id"] in plan["unchanged"]:
            continue
        leads = bootstrap(available_sources, area)
        write_document(
            root / "areas" / f"{slug(area['id'])}.md", area_document(area, args.repo_sha, leads)
        )
    print(json.dumps(plan, indent=2, sort_keys=True))


def validate_finding(value: Any, default_sha: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("each finding must be a JSON object")
    required = (
        "title",
        "question",
        "kind",
        "method",
        "observed_result",
        "conclusion",
        "disposition",
    )
    for key in required:
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"finding requires non-empty {key}")
    if value["disposition"] not in DISPOSITIONS:
        raise ValueError("knowledge accepts only confirmed or disproved findings")
    paths = value.get("evidence_paths", [])
    dependencies = value.get("dependencies", {})
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ValueError("finding evidence_paths must be a list of strings")
    if not isinstance(dependencies, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in dependencies.items()
    ):
        raise ValueError("finding dependencies must map strings to strings")
    result = dict(value)
    identity = json.dumps(
        {"question": result["question"], "kind": result["kind"], "evidence_paths": sorted(paths)},
        sort_keys=True,
        separators=(",", ":"),
    )
    result["id"] = result.get("id") or hashlib.sha256(identity.encode()).hexdigest()[:16]
    result["evidence_paths"] = sorted(set(paths))
    result["dependencies"] = dict(sorted(dependencies.items()))
    result["validated_sha"] = result.get("validated_sha") or default_sha
    return result


def update(args: argparse.Namespace) -> None:
    root = knowledge_root(args)
    path = root / "areas" / f"{slug(args.area)}.md"
    document = parse_document(path)
    if document["revision"] != args.expected_revision:
        raise RuntimeError(
            f"knowledge revision conflict: expected {args.expected_revision}, found {document['revision']}"
        )
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    raw = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise ValueError("knowledge update must contain a findings list")
    merged = {finding["id"]: finding for finding in document.get("findings", [])}
    for value in raw:
        finding = validate_finding(value, args.repo_sha)
        merged[finding["id"]] = finding
    document["findings"] = sorted(merged.values(), key=lambda item: (item["title"], item["id"]))
    document["bootstrap_leads"] = [
        lead for lead in document.get("bootstrap_leads", []) if lead.get("id") not in merged
    ]
    document["source_sha"] = args.repo_sha
    document["revision"] += 1
    document["updated_at"] = utc_now()
    write_document(path, document)
    print(
        json.dumps(
            {
                "area": args.area,
                "revision": document["revision"],
                "findings": len(document["findings"]),
            }
        )
    )


def context(args: argparse.Namespace) -> None:
    root = knowledge_root(args)
    document = parse_document(root / "areas" / f"{slug(args.area)}.md")
    versions: dict[str, str] = {}
    if args.versions:
        value = json.loads(args.versions.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
        ):
            raise ValueError("versions must be a JSON object mapping strings to strings")
        versions = value
    findings = []
    for finding in document.get("findings", []):
        dependencies = finding.get("dependencies", {})
        reusable = (
            finding["kind"] in REUSABLE_KINDS
            and bool(dependencies)
            and all(versions.get(name) == version for name, version in dependencies.items())
        )
        findings.append({**finding, "reuse": "reusable" if reusable else "recheck"})
    print(
        json.dumps(
            {
                "area": args.area,
                "revision": document["revision"],
                "findings": findings,
                "bootstrap_leads": document.get("bootstrap_leads", []),
            },
            indent=2,
            sort_keys=True,
        )
    )


def show(args: argparse.Namespace) -> None:
    root = knowledge_root(args)
    active = active_documents(root)
    requested_area = getattr(args, "area", None)
    if requested_area:
        area = requested_area
        slug(area)
        if area not in active:
            raise ValueError(f"knowledge area does not exist: {area}")
        _, document = active[area]
        print(
            json.dumps(
                {
                    "area": document["area"],
                    "revision": document["revision"],
                    "findings": document.get("findings", []),
                    "bootstrap_leads": document.get("bootstrap_leads", []),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    invalidated = []
    for path in sorted((root / "invalidated").glob("*.md")):
        document = parse_document(path)
        invalidated.append(document["area"]["id"])
    print(
        json.dumps(
            {
                "active": [
                    {
                        "area": area,
                        "revision": document["revision"],
                        "findings": len(document.get("findings", [])),
                    }
                    for area, (_, document) in active.items()
                ],
                "invalidated": sorted(set(invalidated)),
            },
            indent=2,
            sort_keys=True,
        )
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    command = sub.add_parser("status")
    command.add_argument("--project-dir", type=Path)
    command.set_defaults(handler=status)
    command = sub.add_parser("reconcile")
    command.add_argument("--project-dir", type=Path)
    command.add_argument("--areas", type=Path, required=True)
    command.add_argument("--repo-sha", required=True)
    command.set_defaults(handler=reconcile)
    command = sub.add_parser("update")
    command.add_argument("--project-dir", type=Path)
    command.add_argument("--area", required=True)
    command.add_argument("--input", type=Path, required=True)
    command.add_argument("--repo-sha", required=True)
    command.add_argument("--expected-revision", type=int, required=True)
    command.set_defaults(handler=update)
    command = sub.add_parser("context")
    command.add_argument("--project-dir", type=Path)
    command.add_argument("--area", required=True)
    command.add_argument("--versions", type=Path)
    command.set_defaults(handler=context)
    command = sub.add_parser("show")
    command.add_argument("--project-dir", type=Path)
    command.add_argument("--area")
    command.set_defaults(handler=show)
    return root


def main() -> int:
    args = parser().parse_args()
    args.handler(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as error:
        print(f"audit-knowledge: {error}", file=sys.stderr)
        raise SystemExit(2)
