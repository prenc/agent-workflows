"""Install and operate the agent-workflows extension."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
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


def load_json(raw: str) -> Any:
    if raw == "-":
        return json.load(sys.stdin)
    candidate = Path(raw)
    try:
        is_file = candidate.is_file()
    except OSError:
        is_file = False
    return json.loads(candidate.read_text(encoding="utf-8")) if is_file else json.loads(raw)


def load_request(raw: str) -> dict[str, Any]:
    value = load_json(raw)
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    return value


def add_feedback_commands(feedback_parser: argparse.ArgumentParser, *, agent_output: bool) -> None:
    """Configure the human or JSON-only feedback command surface."""
    feedback_parser.set_defaults(agent_interface=agent_output)
    feedback_commands = feedback_parser.add_subparsers(dest="feedback_command", required=True)
    feedback_add = feedback_commands.add_parser("add", help="record one concise observation")
    feedback_add.add_argument("message", help="PHI-free workflow friction and its consequence")
    feedback_add.add_argument("--tool", help="related native or external tool name")
    feedback_add.set_defaults(json_output=agent_output)
    if not agent_output:
        feedback_commands.add_parser("path", help="print the feedback JSONL path")
    feedback_summary = feedback_commands.add_parser(
        "summary", help="summarize the complete feedback state"
    )
    feedback_summary.add_argument("--repository")
    feedback_summary.add_argument("--workflow")
    feedback_summary.add_argument(
        "--since", help="include records from the last AGE, such as 24h, 30d, or 4w"
    )
    if agent_output:
        feedback_summary.set_defaults(json_output=True)
    else:
        feedback_summary.add_argument(
            "--json", action="store_true", dest="json_output", help="print machine-readable JSON"
        )
    feedback_list = feedback_commands.add_parser(
        "list",
        aliases=["ls"],
        help="list newest feedback summaries",
        description=(
            "List newest feedback as one compact record per line. Defaults to the 50 newest "
            "open records; use 'agent-feedback show REF...' for complete "
            "record details."
            if agent_output
            else "List newest feedback as one compact record per line. Defaults to the 50 "
            "newest open records; use 'feedback show REF...' for complete record details."
        ),
    )
    feedback_list.set_defaults(feedback_command="list")
    feedback_list.add_argument("--repository", help="include only this OWNER/REPO")
    feedback_list.add_argument("--workflow", help="include only this workflow")
    feedback_list.add_argument(
        "--since", help="include records from the last AGE, such as 24h, 30d, or 4w"
    )
    feedback_list.add_argument(
        "--status",
        choices=("open", "closed", "all"),
        default="open",
        help="record state to include (default: open)",
    )
    feedback_list.add_argument(
        "--source",
        action="append",
        dest="sources",
        help="include this normalized source; repeat to include more than one",
    )
    list_limit = feedback_list.add_mutually_exclusive_group()
    list_limit.add_argument(
        "--limit", type=int, default=50, help="maximum newest records to return (default: 50)"
    )
    list_limit.add_argument(
        "--all",
        action="store_true",
        dest="all_records",
        help="return every matching record instead of limiting to 50",
    )
    if agent_output:
        feedback_list.set_defaults(json_output=True)
    else:
        feedback_list.add_argument(
            "--json",
            action="store_true",
            dest="json_output",
            help="print the same compact records as a JSON array",
        )
    feedback_show = feedback_commands.add_parser(
        "show", help="show one or more complete feedback records"
    )
    feedback_show.add_argument("feedback_ids", nargs="+")
    feedback_trace = feedback_commands.add_parser(
        "trace", help="locate feedback in Qwen transcripts without printing conversation content"
    )
    feedback_trace.add_argument("feedback_id")
    if agent_output:
        feedback_trace.set_defaults(json_output=True)
    else:
        feedback_trace.add_argument(
            "--json", action="store_true", dest="json_output", help="print machine-readable JSON"
        )
    feedback_close = feedback_commands.add_parser(
        "close", help="close reviewed feedback without deleting it"
    )
    feedback_close.add_argument("feedback_ids", nargs="*")
    feedback_close.add_argument(
        "--disposition",
        choices=sorted(feedback.RESOLUTION_DISPOSITIONS),
        help="how the feedback was resolved (default: addressed)",
    )
    feedback_close.add_argument(
        "--note",
        help="optional short PHI-free resolution note",
    )
    feedback_close.add_argument("--input", help="JSON resolution list, JSON file, or - for stdin")
    if agent_output:
        feedback_close.set_defaults(json_output=True)
    else:
        feedback_close.add_argument(
            "--json", action="store_true", dest="json_output", help="print machine-readable JSON"
        )
    feedback_reopen = feedback_commands.add_parser("reopen", help="reopen closed feedback")
    feedback_reopen.add_argument("feedback_ids", nargs="+")
    feedback_reopen.set_defaults(json_output=agent_output)
    if not agent_output:
        feedback_remove = feedback_commands.add_parser(
            "remove", help="permanently remove reviewed feedback records"
        )
        feedback_remove.add_argument("feedback_ids", nargs="+")


class AgentArgumentParser(argparse.ArgumentParser):
    """Turn agent-facing usage errors into structured operational errors."""

    def error(self, message: str) -> None:
        raise ValueError(message)


def build_agent_feedback_parser() -> argparse.ArgumentParser:
    parser = AgentArgumentParser(
        prog="agent-feedback",
        description="Operate the local feedback queue with JSON-only output.",
    )
    add_feedback_commands(parser, agent_output=True)
    return parser


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
    add_feedback_commands(feedback_parser, agent_output=False)

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
        handler = getattr(runtime, method)
        result = (
            handler(request, invocation_id=f"cli:{uuid.uuid4()}")
            if method == "task_manage"
            else handler(request)
        )
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
        print(
            json.dumps(result, indent=2, sort_keys=True)
            if getattr(args, "json_output", False)
            else f"Recorded feedback {result['ref']}."
        )
        return 0
    if args.feedback_command == "path":
        print(feedback.storage_path())
        return 0
    if args.feedback_command == "show":
        records = feedback.find_many(args.feedback_ids)
        result: Any = records[0] if len(records) == 1 else records
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
    if args.feedback_command == "summary":
        result = feedback.feedback_summary(
            repository=args.repository,
            workflow=args.workflow,
            cutoff=feedback.relative_cutoff(args.since),
        )
        print(
            json.dumps(result, indent=2, sort_keys=True)
            if args.json_output
            else feedback.format_feedback_summary(result)
        )
        return 0
    if args.feedback_command == "close":
        if args.input is not None:
            if args.feedback_ids or args.disposition is not None or args.note is not None:
                raise ValueError(
                    "feedback close --input cannot be combined with IDs, --disposition, or --note"
                )
            result = feedback.resolve_records(load_json(args.input))
            print(
                json.dumps(result, indent=2, sort_keys=True)
                if args.json_output
                else f"Closed {result['changed']} feedback record"
                f"{'s' if result['changed'] != 1 else ''}; {result['unchanged']} unchanged."
            )
            return 0
        if not args.feedback_ids:
            raise ValueError("feedback close requires IDs or --input")
        if getattr(args, "agent_interface", False):
            resolution: dict[str, str] = {
                "disposition": args.disposition or "addressed",
            }
            if args.note is not None:
                resolution["note"] = args.note
            result = feedback.resolve_records(
                [
                    {"ref": feedback_id, **resolution}
                    for feedback_id in dict.fromkeys(args.feedback_ids)
                ]
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        changed = feedback.set_closed(
            args.feedback_ids,
            closed=True,
            disposition=args.disposition or "addressed",
            note=args.note,
        )
        print(f"Closed {len(changed)} feedback record{'s' if len(changed) != 1 else ''}.")
        return 0
    if args.feedback_command == "reopen":
        changed = feedback.set_closed(args.feedback_ids, closed=False)
        print(
            json.dumps(
                {"changed": len(changed), "feedback_ids": changed},
                indent=2,
                sort_keys=True,
            )
            if getattr(args, "json_output", False)
            else f"Reopened {len(changed)} feedback record{'s' if len(changed) != 1 else ''}."
        )
        return 0
    if args.feedback_command == "remove":
        removed = feedback.remove(args.feedback_ids)
        print(f"Removed {len(removed)} feedback record{'s' if len(removed) != 1 else ''}.")
        return 0
    else:
        limit = None if args.all_records else args.limit
        if limit is not None and limit < 1:
            raise ValueError("feedback limit must be positive")
        result = feedback.compact_records(
            repository=args.repository,
            workflow=args.workflow,
            sources=args.sources,
            status=args.status,
            cutoff=feedback.relative_cutoff(args.since),
            limit=limit,
        )
    if getattr(args, "json_output", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        width = shutil.get_terminal_size(fallback=(140, 24)).columns
        print(feedback.format_compact_records(result, width=width))
    return 0


def run_agent_feedback(arguments: list[str]) -> int:
    """Run the bounded agent feedback interface with structured errors."""
    try:
        args = build_agent_feedback_parser().parse_args(arguments)
        return run_feedback(args)
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2


def agent_feedback_main() -> int:
    """Run the standalone JSON-only feedback command."""
    return run_agent_feedback(sys.argv[1:])


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
