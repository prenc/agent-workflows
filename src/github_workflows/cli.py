"""Install and operate the agent-workflows extension."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from . import feedback
from .installation import add_install_arguments, install_from_args
from .mcp_server import create_server
from .models import (
    AuditRecordRequest,
    HistoryManageRequest,
    HistoryQueryRequest,
    InventoryRequest,
    KnowledgeRequest,
    ProbeRequest,
    PublishRequest,
    RunManageRequest,
    TaskManageRequest,
)
from .runtime import WorkflowRuntime

REQUESTS = {
    "run-manage": (RunManageRequest, "run_manage"),
    "task-manage": (TaskManageRequest, "task_manage"),
    "history-manage": (HistoryManageRequest, "history_manage"),
    "history-query": (HistoryQueryRequest, "history_query"),
    "audit-inventory": (InventoryRequest, "audit_inventory"),
    "audit-knowledge": (KnowledgeRequest, "audit_knowledge"),
    "audit-probe": (ProbeRequest, "audit_probe"),
    "audit-record": (AuditRecordRequest, "audit_record"),
    "audit-publish": (PublishRequest, "audit_publish"),
}


def load_request(raw: str) -> dict[str, Any]:
    if raw == "-":
        value = json.load(sys.stdin)
    else:
        candidate = Path(raw)
        value = (
            json.loads(candidate.read_text(encoding="utf-8"))
            if candidate.is_file()
            else json.loads(raw)
        )
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install", help="install or update agent integrations")
    add_install_arguments(install)

    mcp = subparsers.add_parser("mcp", help="serve the private Qwen MCP interface")
    mcp.add_argument("--workspace", type=Path, required=True)
    mcp.add_argument("--project-dir", type=Path)

    workflow = subparsers.add_parser("workflow", help="run a manual workflow recovery operation")
    workflow.add_argument("--workspace", type=Path, default=Path.cwd())
    workflow.add_argument("--project-dir", type=Path)
    workflow.add_argument(
        "tool",
        choices=sorted([*REQUESTS, "run-status", "task-context", "audit-metrics"]),
    )
    workflow.add_argument("request", nargs="?", help="JSON object, JSON file, or - for stdin")

    feedback_parser = subparsers.add_parser("feedback", help="inspect local agent feedback")
    feedback_commands = feedback_parser.add_subparsers(dest="feedback_command", required=True)
    feedback_commands.add_parser("path", help="print the feedback JSONL path")
    feedback_list = feedback_commands.add_parser("list", help="list compact feedback records")
    feedback_list.add_argument("--repository")
    feedback_list.add_argument("--workflow")
    feedback_list.add_argument("--tool")
    feedback_list.add_argument("--limit", type=int, default=50)
    feedback_list.add_argument(
        "--json", action="store_true", dest="json_output", help="print machine-readable JSON"
    )
    feedback_show = feedback_commands.add_parser("show", help="show one complete feedback record")
    feedback_show.add_argument("feedback_id")
    return parser


def run_workflow(args: argparse.Namespace) -> int:
    runtime = WorkflowRuntime(args.workspace, args.project_dir)
    if args.tool == "run-status":
        if not args.request:
            raise ValueError("run-status requires a workflow name")
        result = runtime.run_status(args.request)
    elif args.tool == "task-context":
        if not args.request:
            raise ValueError("task-context requires a namespaced task reference")
        result = runtime.task_context(args.request)
    elif args.tool == "audit-metrics":
        result = runtime.audit_metrics()
    else:
        if not args.request:
            raise ValueError(f"{args.tool} requires a JSON request")
        model, method = REQUESTS[args.tool]
        request = model.model_validate(load_request(args.request))
        result = getattr(runtime, method)(request)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def run_feedback(args: argparse.Namespace) -> int:
    if args.feedback_command == "path":
        print(feedback.storage_path())
        return 0
    if args.feedback_command == "show":
        result: Any = feedback.find(args.feedback_id)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    else:
        if args.limit < 1:
            raise ValueError("feedback limit must be positive")
        result = feedback.compact_records(
            repository=args.repository,
            workflow=args.workflow,
            tool=args.tool,
            limit=args.limit,
        )
    if getattr(args, "json_output", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            feedback.format_table(
                result,
                width=shutil.get_terminal_size(fallback=(140, 24)).columns,
            )
        )
    return 0


def main() -> int:
    try:
        args = build_parser().parse_args()
        if args.command == "install":
            return install_from_args(args)
        if args.command == "mcp":
            runtime = WorkflowRuntime(args.workspace, args.project_dir)
            create_server(runtime).run()
            return 0
        if args.command == "feedback":
            return run_feedback(args)
        return run_workflow(args)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"agent-workflows: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
