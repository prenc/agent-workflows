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
    WorkflowFeedbackRequest,
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

    feedback_parser = subparsers.add_parser(
        "feedback", help="record or inspect local agent feedback"
    )
    feedback_commands = feedback_parser.add_subparsers(dest="feedback_command", required=True)
    feedback_add = feedback_commands.add_parser("add", help="record one concise observation")
    feedback_add.add_argument("message", help="PHI-free workflow friction and its consequence")
    feedback_add.add_argument("--tool", help="related native or external tool name")
    feedback_commands.add_parser("path", help="print the feedback JSONL path")
    feedback_stats = feedback_commands.add_parser(
        "stats", help="summarize feedback retention and record sizes"
    )
    feedback_stats.add_argument(
        "--json", action="store_true", dest="json_output", help="print machine-readable JSON"
    )
    feedback_list = feedback_commands.add_parser(
        "list", aliases=["ls"], help="list compact feedback records"
    )
    feedback_list.set_defaults(feedback_command="list")
    feedback_list.add_argument("--repository")
    feedback_list.add_argument("--workflow")
    feedback_list.add_argument(
        "--closed", action="store_true", help="show closed feedback instead of open feedback"
    )
    feedback_list.add_argument(
        "--source",
        "--tool",
        action="append",
        dest="sources",
        help="include a logical source; repeat to include more than one",
    )
    feedback_list.add_argument("--limit", type=int, default=50)
    feedback_list.add_argument(
        "--json", action="store_true", dest="json_output", help="print machine-readable JSON"
    )
    feedback_sources = feedback_commands.add_parser(
        "sources", help="list unique logical feedback sources"
    )
    feedback_sources.add_argument("--repository")
    feedback_sources.add_argument("--workflow")
    feedback_sources.add_argument(
        "--closed", action="store_true", help="count closed feedback instead of open feedback"
    )
    feedback_sources.add_argument(
        "--json", action="store_true", dest="json_output", help="print machine-readable JSON"
    )
    feedback_show = feedback_commands.add_parser("show", help="show one complete feedback record")
    feedback_show.add_argument("feedback_id")
    feedback_trace = feedback_commands.add_parser(
        "trace", help="locate feedback in Qwen transcripts without printing conversation content"
    )
    feedback_trace.add_argument("feedback_id")
    feedback_trace.add_argument(
        "--json", action="store_true", dest="json_output", help="print machine-readable JSON"
    )
    feedback_close = feedback_commands.add_parser(
        "close", help="close reviewed feedback without deleting it"
    )
    feedback_close.add_argument("feedback_ids", nargs="+")
    feedback_close.add_argument(
        "--disposition",
        choices=sorted(feedback.RESOLUTION_DISPOSITIONS),
        default="addressed",
        help="how the feedback was resolved (default: addressed)",
    )
    feedback_close.add_argument(
        "--note",
        help="optional short PHI-free resolution note",
    )
    feedback_reopen = feedback_commands.add_parser("reopen", help="reopen closed feedback")
    feedback_reopen.add_argument("feedback_ids", nargs="+")
    feedback_remove = feedback_commands.add_parser(
        "remove", help="permanently remove reviewed feedback records"
    )
    feedback_remove.add_argument("feedback_ids", nargs="+")
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
    if args.feedback_command == "add":
        request = WorkflowFeedbackRequest(message=args.message, tool=args.tool)
        result = feedback.append_manual(
            message=request.message,
            tool=request.tool,
            workspace=Path.cwd(),
        )
        print(f"Recorded feedback {result['feedback_id']}.")
        return 0
    if args.feedback_command == "path":
        print(feedback.storage_path())
        return 0
    if args.feedback_command == "show":
        result: Any = feedback.find(args.feedback_id)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.feedback_command == "trace":
        result = feedback.trace(args.feedback_id)
        print(
            json.dumps(result, indent=2, sort_keys=True)
            if args.json_output
            else feedback.format_trace(result)
        )
        return 0
    if args.feedback_command == "stats":
        result = feedback.storage_stats()
        print(
            json.dumps(result, indent=2, sort_keys=True)
            if args.json_output
            else feedback.format_stats(result)
        )
        return 0
    if args.feedback_command in {"close", "reopen"}:
        closed = args.feedback_command == "close"
        changed = feedback.set_closed(
            args.feedback_ids,
            closed=closed,
            disposition=getattr(args, "disposition", "addressed"),
            note=getattr(args, "note", None),
        )
        action = "Closed" if closed else "Reopened"
        print(f"{action} {len(changed)} feedback record{'s' if len(changed) != 1 else ''}.")
        return 0
    if args.feedback_command == "remove":
        removed = feedback.remove(args.feedback_ids)
        print(f"Removed {len(removed)} feedback record{'s' if len(removed) != 1 else ''}.")
        return 0
    if args.feedback_command == "sources":
        result = feedback.source_counts(
            repository=args.repository,
            workflow=args.workflow,
            closed=args.closed,
        )
        if args.json_output:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(feedback.format_sources(result))
        return 0
    else:
        if args.limit < 1:
            raise ValueError("feedback limit must be positive")
        result = feedback.compact_records(
            repository=args.repository,
            workflow=args.workflow,
            sources=args.sources,
            closed=args.closed,
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
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"agent-workflows: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
