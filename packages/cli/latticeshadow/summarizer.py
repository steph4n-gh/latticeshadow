"""Local-first summarization helpers for timeline context."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from latticeshadow.sensitivity import classify, redact


def _clean_text(text: str) -> str:
    sensitivity = classify(text)
    if sensitivity == "sensitive":
        return "[SENSITIVE CONTENT OMITTED]"
    if sensitivity == "unknown":
        return redact(text)
    return text


def _foundation_models_summary(lines: list[str]) -> str | None:
    command = os.environ.get("LATTICESHADOW_FOUNDATION_MODELS_CMD")
    if not command:
        return None
    payload = json.dumps({"task": "summarize", "events": lines})
    try:
        result = subprocess.run(
            command.split(),
            input=payload,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        return None
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def _llm_summary(lines: list[str]) -> str | None:
    try:
        from latticeshadow.llm import ShadowLLM

        llm = ShadowLLM.from_config()
        if not llm:
            return None
        return llm.complete(
            system="Summarize local private memory context. Be concise and cite visible event ids.",
            user="\n".join(lines),
            temperature=0.2,
        )
    except Exception:
        return None


def _extractive_summary(events: list[dict[str, Any]], lines: list[str]) -> str:
    if not events:
        return "No memory events found."
    types: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("type") or "event")
        types[event_type] = types.get(event_type, 0) + 1
    type_text = ", ".join(f"{name}={count}" for name, count in sorted(types.items()))
    lead = lines[0] if lines else ""
    return f"{len(events)} event(s): {type_text}. Most recent: {lead}"


def summarize_events(events: list[dict[str, Any]], prefer_foundation: bool = True) -> dict[str, Any]:
    lines = []
    for event in events:
        text = _clean_text(str(event.get("text") or ""))
        text = " ".join(text.split())
        if len(text) > 300:
            text = text[:297] + "..."
        lines.append(f"{event.get('id')} [{event.get('type')}]: {text}")

    provider = "extractive"
    summary = None
    if prefer_foundation:
        summary = _foundation_models_summary(lines)
        if summary:
            provider = "foundation_models"
    if not summary:
        summary = _llm_summary(lines)
        if summary:
            provider = "llm"
    if not summary:
        summary = _extractive_summary(events, lines)

    return {
        "schema": "latticeshadow.summary.v1",
        "provider": provider,
        "summary": summary,
        "event_count": len(events),
    }
