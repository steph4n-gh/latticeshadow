"""Normalized memory timeline helpers for the CLI.

This layer is intentionally additive: it reads existing LatticeDB rows and
metadata JSON without changing the SQLite schema.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Iterable

EVENT_TYPES = {
    "clipboard",
    "terminal",
    "ambient",
    "file",
    "url",
    "app",
    "repair",
    "sync",
    "model_call",
}

EVENT_PREFIXES = {
    "clipboard": "clip",
    "terminal": "cmd",
    "ambient": "amb",
    "file": "file",
    "url": "url",
    "app": "app",
    "repair": "repair",
    "sync": "sync",
    "model_call": "model",
}


def _parse_metadata(raw: str | bytes | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _decrypt_document(vault: Any, document: Any) -> str:
    if document is None:
        return ""
    text = str(document)
    privacy = getattr(vault, "_privacy", None)
    if privacy and text.startswith("enc:"):
        try:
            return privacy.decrypt_document(text)
        except Exception:
            return "[encrypted document unavailable]"
    return text


def _infer_event_type(doc_id: str, metadata: dict[str, Any]) -> str:
    source = str(metadata.get("event_type") or metadata.get("source") or "").lower()
    if source in EVENT_TYPES:
        return source
    if doc_id.startswith("cmd_"):
        return "terminal"
    if doc_id.startswith("clip_"):
        return "clipboard"
    if metadata.get("url"):
        return "url"
    if metadata.get("app") or metadata.get("application"):
        return "app"
    return "clipboard"


def _row_to_event(vault: Any, row: tuple[Any, ...], score: float | None = None) -> dict[str, Any]:
    doc_id, document, metadata_json, collection, created_at, last_accessed = row
    metadata = _parse_metadata(metadata_json)
    text = _decrypt_document(vault, document)
    event_type = _infer_event_type(str(doc_id), metadata)
    event = {
        "id": str(doc_id),
        "type": event_type,
        "source": metadata.get("source", event_type),
        "collection": collection,
        "timestamp": created_at,
        "last_accessed": last_accessed,
        "text": text,
        "metadata": metadata,
    }
    if score is not None:
        event["score"] = float(score)
    return event


def _rows_by_id(vault: Any, ids: Iterable[str]) -> dict[str, tuple[Any, ...]]:
    ids = [doc_id for doc_id in ids if doc_id]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    collection = getattr(vault, "name", "clipboard")
    with vault._store._connect() as conn:
        cursor = conn.execute(
            f"""SELECT doc_id, document, metadata_json, collection, created_at, last_accessed
                FROM vectors
                WHERE collection = ? AND doc_id IN ({placeholders})""",
            [collection, *ids],
        )
        return {str(row[0]): row for row in cursor.fetchall()}


def fetch_events(
    vault: Any,
    limit: int = 20,
    source: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    collection = getattr(vault, "name", "clipboard")
    clauses = ["collection = ?"]
    params: list[Any] = [collection]
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    if until:
        clauses.append("created_at <= ?")
        params.append(until)

    overfetch = max(limit * 5, limit)
    with vault._store._connect() as conn:
        cursor = conn.execute(
            f"""SELECT doc_id, document, metadata_json, collection, created_at, last_accessed
                FROM vectors
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC
                LIMIT ?""",
            [*params, overfetch],
        )
        events = [_row_to_event(vault, row) for row in cursor.fetchall()]

    if source:
        wanted = source.lower()
        events = [
            event for event in events
            if str(event.get("type", "")).lower() == wanted
            or str(event.get("source", "")).lower() == wanted
        ]
    return events[:limit]


def search_events(vault: Any, query: str, limit: int = 10) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    result = vault.search(query, n_results=limit, hybrid=True)
    ids = list(getattr(result, "ids", []) or [])
    documents = list(getattr(result, "documents", []) or [])
    metadatas = list(getattr(result, "metadatas", []) or [])
    scores = list(getattr(result, "scores", []) or [])
    rows = _rows_by_id(vault, ids)

    events: list[dict[str, Any]] = []
    for idx, doc_id in enumerate(ids):
        if doc_id in rows:
            event = _row_to_event(
                vault,
                rows[doc_id],
                scores[idx] if idx < len(scores) else None,
            )
        else:
            metadata = metadatas[idx] if idx < len(metadatas) and isinstance(metadatas[idx], dict) else {}
            document = documents[idx] if idx < len(documents) else ""
            event = {
                "id": doc_id,
                "type": _infer_event_type(doc_id, metadata),
                "source": metadata.get("source", ""),
                "collection": getattr(vault, "name", "clipboard"),
                "timestamp": "",
                "last_accessed": "",
                "text": document,
                "metadata": metadata,
            }
            if idx < len(scores):
                event["score"] = float(scores[idx])
        events.append(event)
    return events


def add_event(
    vault: Any,
    event_type: str,
    text: str,
    metadata: dict[str, Any] | None = None,
    doc_id: str | None = None,
) -> str:
    event_type = event_type.lower()
    if event_type not in EVENT_TYPES:
        raise ValueError(f"event_type must be one of {', '.join(sorted(EVENT_TYPES))}")
    metadata = dict(metadata or {})
    metadata.setdefault("event_type", event_type)
    metadata.setdefault("source", event_type)
    metadata.setdefault("captured_at", datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
    if not doc_id:
        prefix = EVENT_PREFIXES[event_type]
        doc_id = f"{prefix}_{int(time.time() * 1000)}"
    vault.add(documents=[text], ids=[doc_id], metadatas=[metadata])
    return doc_id


def forget_events(vault: Any, ids: Iterable[str]) -> int:
    ids = [doc_id for doc_id in ids if doc_id]
    if not ids:
        return 0
    return int(vault.delete(ids))


def open_target(event: dict[str, Any]) -> str | None:
    metadata = event.get("metadata", {}) or {}
    for key in ("url", "file", "path"):
        value = metadata.get(key)
        if value:
            return str(value)
    text = str(event.get("text", "")).strip()
    if text.startswith(("http://", "https://", "file://")):
        return text.splitlines()[0]
    return None


def format_event(event: dict[str, Any], width: int = 180) -> str:
    timestamp = str(event.get("timestamp") or "")
    event_type = str(event.get("type") or "event")
    doc_id = str(event.get("id") or "")
    score = event.get("score")
    score_text = f" score={score:.3f}" if isinstance(score, float) else ""
    text = " ".join(str(event.get("text") or "").split())
    if len(text) > width:
        text = text[: max(0, width - 3)] + "..."
    return f"{timestamp} [{event_type}] {doc_id}{score_text} {text}".strip()


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for event in events:
        key = str(event.get("type") or "event")
        counts[key] = counts.get(key, 0) + 1
    return {
        "count": len(events),
        "types": counts,
        "newest": events[0].get("timestamp") if events else None,
        "oldest": events[-1].get("timestamp") if events else None,
    }


def current_context(vault: Any, limit: int = 20) -> dict[str, Any]:
    events = fetch_events(vault, limit=limit)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "summary": summarize_events(events),
        "events": events,
    }
