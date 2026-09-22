"""Minimal MCP stdio bridge for LatticeShadow memory.

The server exposes redacted timeline/context resources and a small set of tools.
It intentionally stays thin: every action maps back to existing CLI helpers.
"""

from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from typing import Any, Callable, TextIO

from latticeshadow.moonshot import generate_privacy_report
from latticeshadow.repair_queue import create_repair_proposal, list_repairs
from latticeshadow.sensitivity import redact
from latticeshadow.summarizer import summarize_events as summarize_memory_events
from latticeshadow.timeline import current_context, fetch_events, forget_events, search_events

VaultFactory = Callable[[], Any]
Forgetter = Callable[[list[str]], Any]

PROTOCOL_VERSION = "2025-06-18"


def _redact_event(event: dict[str, Any]) -> dict[str, Any]:
    clean = dict(event)
    if "text" in clean:
        clean["text"] = redact(str(clean.get("text") or ""))
    metadata = clean.get("metadata")
    if isinstance(metadata, dict):
        clean["metadata"] = {
            str(key): redact(str(value)) if isinstance(value, str) else value
            for key, value in metadata.items()
        }
    return clean


def _redact_context(payload: dict[str, Any]) -> dict[str, Any]:
    clean = dict(payload)
    clean["events"] = [_redact_event(event) for event in payload.get("events", [])]
    return clean


def _text(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        body = payload
    else:
        body = json.dumps(payload, indent=2, sort_keys=True)
    return {"content": [{"type": "text", "text": body}]}


def _tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "latticeshadow.recall",
            "description": "Redacted semantic recall over local LatticeShadow memory.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["query"],
            },
        },
        {
            "name": "latticeshadow.current_context",
            "description": "Recent redacted memory timeline context.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        },
        {
            "name": "latticeshadow.privacy_report",
            "description": "Local privacy posture report for vault files and listeners.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "latticeshadow.summarize",
            "description": "Extractive summary of recent redacted local memory events.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
            },
        },
        {
            "name": "latticeshadow.create_repair_proposal",
            "description": "Create a pending human-approved repair proposal.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "command": {"type": "string"},
                    "risk": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["summary"],
            },
        },
        {
            "name": "latticeshadow.forget",
            "description": "Delete specific local memory ids after explicit confirmation.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "string"}},
                    "confirm": {"type": "string"},
                },
                "required": ["ids", "confirm"],
            },
        },
    ]


def _resource_specs() -> list[dict[str, Any]]:
    return [
        {
            "uri": "latticeshadow://timeline/recent",
            "name": "Recent LatticeShadow Timeline",
            "mimeType": "application/json",
        },
        {
            "uri": "latticeshadow://current-context",
            "name": "Current LatticeShadow Context",
            "mimeType": "application/json",
        },
        {
            "uri": "latticeshadow://privacy-report",
            "name": "LatticeShadow Privacy Report",
            "mimeType": "application/json",
        },
        {
            "uri": "latticeshadow://repairs/recent",
            "name": "Recent LatticeShadow Repair Proposals",
            "mimeType": "application/json",
        },
    ]


def _prompt_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "recover-my-context",
            "description": "Recover what the user was doing from recent private memory.",
            "arguments": [{"name": "time_hint", "required": False}],
        },
        {
            "name": "explain-this-error-from-history",
            "description": "Explain an error using prior matching local memory.",
            "arguments": [{"name": "error", "required": True}],
        },
        {
            "name": "draft-next-command",
            "description": "Draft the next terminal command from current context.",
            "arguments": [{"name": "goal", "required": False}],
        },
    ]


def _call_tool(
    name: str,
    arguments: dict[str, Any],
    vault_factory: VaultFactory,
    db_path: str,
    data_dir: str,
    forgetter: Forgetter | None = None,
) -> dict[str, Any]:
    if name == "latticeshadow.recall":
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        limit = int(arguments.get("limit") or 10)
        events = [_redact_event(event) for event in search_events(vault_factory(), query, limit=limit)]
        return _text({"query": query, "events": events})

    if name == "latticeshadow.current_context":
        limit = int(arguments.get("limit") or 20)
        return _text(_redact_context(current_context(vault_factory(), limit=limit)))

    if name == "latticeshadow.privacy_report":
        return _text(generate_privacy_report(db_path, data_dir=data_dir))

    if name == "latticeshadow.summarize":
        limit = int(arguments.get("limit") or 20)
        query = str(arguments.get("query") or "").strip()
        vault = vault_factory()
        events = search_events(vault, query, limit=limit) if query else fetch_events(vault, limit=limit)
        redacted = [_redact_event(event) for event in events]
        summary = summarize_memory_events(redacted)
        return _text({"query": query or None, **summary, "events": redacted[:5]})

    if name == "latticeshadow.create_repair_proposal":
        summary = str(arguments.get("summary") or "").strip()
        if not summary:
            raise ValueError("summary is required")
        proposal = create_repair_proposal(
            summary=summary,
            source=str(arguments.get("source") or "mcp"),
            command=arguments.get("command"),
            risk=str(arguments.get("risk") or "medium"),
            data_dir=data_dir,
        )
        return _text(proposal)

    if name == "latticeshadow.forget":
        ids = [str(item) for item in arguments.get("ids", []) if str(item).strip()]
        if arguments.get("confirm") != "FORGET":
            raise ValueError("confirm must be exactly 'FORGET'")
        if forgetter:
            return _text(forgetter(ids))
        deleted = forget_events(vault_factory(), ids)
        return _text({"deleted": deleted, "ids": ids})

    raise ValueError(f"Unknown tool: {name}")


def _read_resource(uri: str, vault_factory: VaultFactory, db_path: str, data_dir: str) -> dict[str, Any]:
    if uri == "latticeshadow://timeline/recent":
        payload = {"events": [_redact_event(event) for event in fetch_events(vault_factory(), limit=50)]}
    elif uri == "latticeshadow://current-context":
        payload = _redact_context(current_context(vault_factory(), limit=20))
    elif uri == "latticeshadow://privacy-report":
        payload = generate_privacy_report(db_path, data_dir=data_dir)
    elif uri == "latticeshadow://repairs/recent":
        payload = {"repairs": list_repairs(limit=20, data_dir=data_dir)}
    else:
        raise ValueError(f"Unknown resource: {uri}")

    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(payload, indent=2, sort_keys=True),
            }
        ]
    }


def _get_prompt(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "recover-my-context":
        time_hint = arguments.get("time_hint") or "recently"
        text = (
            f"Use the LatticeShadow current-context and timeline resources to recover what I was doing {time_hint}. "
            "Keep the answer grounded in cited memory ids and call out uncertainty."
        )
    elif name == "explain-this-error-from-history":
        error = redact(str(arguments.get("error") or ""))
        text = (
            "Use LatticeShadow recall to find prior matching errors, explain the likely cause, "
            f"and draft a safe next command. Current error:\n{error}"
        )
    elif name == "draft-next-command":
        goal = arguments.get("goal") or "continue the current task"
        text = (
            f"Use redacted LatticeShadow current context to draft the next shell command to {goal}. "
            "Prefer a command that inspects or verifies before mutating."
        )
    else:
        raise ValueError(f"Unknown prompt: {name}")

    return {
        "description": name.replace("-", " "),
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }


def handle_request(
    request: dict[str, Any],
    vault_factory: VaultFactory,
    db_path: str,
    data_dir: str,
    forgetter: Forgetter | None = None,
) -> dict[str, Any] | None:
    if "id" not in request:
        return None  # JSON-RPC notifications never receive responses.
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    try:
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": "latticeshadow", "version": "0.1.0"},
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            }
        elif method == "tools/list":
            result = {"tools": _tool_specs()}
        elif method == "tools/call":
            result = _call_tool(
                str(params.get("name") or ""),
                params.get("arguments") or {},
                vault_factory,
                db_path,
                data_dir,
                forgetter,
            )
        elif method == "resources/list":
            result = {"resources": _resource_specs()}
        elif method == "resources/read":
            result = _read_resource(str(params.get("uri") or ""), vault_factory, db_path, data_dir)
        elif method == "prompts/list":
            result = {"prompts": _prompt_specs()}
        elif method == "prompts/get":
            result = _get_prompt(str(params.get("name") or ""), params.get("arguments") or {})
        else:
            raise ValueError(f"Unsupported method: {method}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32000, "message": str(exc)},
        }


def _read_message(stdin: TextIO) -> dict[str, Any] | None:
    # MCP stdio uses one JSON object per line, without Content-Length headers.
    while line := stdin.readline():
        if line.strip():
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("method"), str):
                raise ValueError("Expected a JSON-RPC request object")
            return request
    return None


def _write_message(stdout: TextIO, response: dict[str, Any]) -> None:
    body = json.dumps(response, separators=(",", ":"))
    stdout.write(body + "\n")
    stdout.flush()


def serve(
    vault_factory: VaultFactory,
    db_path: str,
    data_dir: str,
    forgetter: Forgetter | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    while True:
        try:
            request = _read_message(stdin)
        except (json.JSONDecodeError, ValueError) as exc:
            code = -32700 if isinstance(exc, json.JSONDecodeError) else -32600
            _write_message(stdout, {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": code, "message": str(exc)},
            })
            continue
        if request is None:
            return
        with redirect_stdout(sys.stderr):
            response = handle_request(request, vault_factory, db_path, data_dir, forgetter=forgetter)
        if response is not None:
            _write_message(stdout, response)
