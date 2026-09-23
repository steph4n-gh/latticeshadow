"""Canonical, scoped memory events shared by CLI, desktop and MCP."""
from __future__ import annotations

import base64
import hashlib
import heapq
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

EVENT_TYPES = {"clipboard", "terminal", "ambient", "file", "url", "app", "repair", "sync", "model_call", "note"}
EVENT_PREFIXES = {"clipboard": "clip", "terminal": "cmd", "ambient": "amb", "file": "file", "url": "url", "app": "app", "repair": "repair", "sync": "sync", "model_call": "model", "note": "note"}
MAX_TEXT_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_LABEL_BYTES = 256
MAX_IDS = 1000


def _utc(value: Any, *, legacy: bool = False) -> str:
    if isinstance(value, datetime):
        date = value
    elif legacy and isinstance(value, (int, float)) and not isinstance(value, bool):
        date = datetime.fromtimestamp(value, timezone.utc)
    elif isinstance(value, str):
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be an ISO 8601 time") from exc
    else:
        raise TypeError("timestamp must be a timezone-aware datetime or ISO 8601 string")
    if date.tzinfo is None or date.utcoffset() is None:
        if not legacy:
            raise ValueError("timestamp must include a timezone")
        date = date.replace(tzinfo=timezone.utc)
    return date.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _label(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_LABEL_BYTES:
        raise ValueError(f"{name} must be a nonempty string of at most {MAX_LABEL_BYTES} UTF-8 bytes")
    return value


def _scope(scope: dict[str, Any] | None) -> dict[str, Any]:
    if scope is None:
        scope = {}
    if not isinstance(scope, dict) or set(scope) - {"projects", "sources", "since", "until"}:
        raise ValueError("scope accepts projects, sources, since and until")
    result = {}
    for field in ("projects", "sources"):
        values = scope.get(field)
        if values is not None:
            if not isinstance(values, (tuple, list)) or len(values) > 100:
                raise ValueError(f"{field} must be a list or tuple with at most 100 entries")
            if field == "sources" and any(value is None for value in values):
                raise ValueError("sources cannot contain null")
            values = tuple(_label(value, field, nullable=field == "projects") for value in values)
        result[field] = values
    for field in ("since", "until"):
        result[field] = _utc(scope[field]) if scope.get(field) is not None else None
    if result["since"] and result["until"] and result["since"] >= result["until"]:
        raise ValueError("scope since must be earlier than until")
    return result


def _metadata(raw: str | bytes | None) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("Stored event metadata must be an object")
    return value


def _event(vault: Any, record: dict[str, Any], *, decrypt: bool = True) -> dict[str, Any]:
    meta = _metadata(record["metadata_json"])
    doc_id = str(record["doc_id"])
    event_type = str(meta.get("event_type") or meta.get("source") or "").lower()
    if event_type not in EVENT_TYPES:
        event_type = "terminal" if doc_id.startswith("cmd_") else "clipboard"
    source = meta.get("source")
    if not isinstance(source, str) or not source:
        source = "unknown"
    captured_at = _utc(meta.get("captured_at") or record["created_at"], legacy=True)
    occurrence = meta.get("timestamp")
    timestamp = _utc(occurrence if occurrence is not None else record["created_at"], legacy=True)
    meta = dict(meta)
    meta["timestamp_inferred"] = bool(meta.get("timestamp_inferred", occurrence is None))
    project = meta.get("project")
    if project is not None and not isinstance(project, str):
        raise ValueError(f"Stored event {doc_id} has invalid project metadata")
    document = (record["document"] or "") if decrypt else ""
    if decrypt and document.startswith("enc:"):
        privacy = getattr(vault, "_privacy", None)
        if privacy is None:
            raise ValueError(f"Encrypted event {doc_id} cannot be decrypted")
        document = privacy.decrypt_document(document)
    return {"id": doc_id, "type": event_type, "source": source,
            "collection": record["collection"], "timestamp": timestamp,
            "captured_at": captured_at, "project": project,
            "last_accessed": record["last_accessed"], "text": document,
            "metadata": meta}


def _matches(event: dict[str, Any], selected: dict[str, Any]) -> bool:
    return (selected["projects"] is None or event["project"] in selected["projects"]) and \
           (selected["sources"] is None or event["source"] in selected["sources"]) and \
           (selected["since"] is None or event["timestamp"] >= selected["since"]) and \
           (selected["until"] is None or event["timestamp"] < selected["until"])


def event_matches_scope(event: dict[str, Any], scope: dict[str, Any] | None) -> bool:
    """Apply the same scope semantics to cached candidates and live records."""
    return _matches(event, _scope(scope))


def iter_events(vault: Any, *, scope: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    """Scan canonical rows in bounded pages, applying scope before ranking."""
    selected = _scope(scope)
    if selected["projects"] == () or selected["sources"] == ():
        return
    cursor = 0
    while True:
        records, following = vault.scan_records(after_row_id=cursor, limit=500)
        for record in records:
            if _matches(_event(vault, record, decrypt=False), selected):
                yield _event(vault, record)
        if following is None:
            return
        cursor = following


def _scope_digest(scope: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:16]


def _make_cursor(timestamp: str, doc_id: str, digest: str) -> str:
    raw = json.dumps([timestamp, doc_id, digest], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _read_cursor(cursor: str, digest: str) -> tuple[str, str]:
    if not isinstance(cursor, str) or len(cursor) > 1024:
        raise ValueError("invalid timeline cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 3 or value[2] != digest:
            raise ValueError
        return _utc(value[0]), _label(value[1], "cursor ID")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("invalid or mismatched timeline cursor") from exc


def fetch_events(vault: Any, *, scope: dict[str, Any] | None = None,
                 limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
    if not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("timeline limit must be between 1 and 500")
    selected = _scope(scope)
    digest = _scope_digest(selected)
    marker = _read_cursor(cursor, digest) if cursor is not None else None
    candidates = iter_events(vault, scope=selected)
    if marker:
        candidates = (event for event in candidates
                      if (event["timestamp"], event["id"]) < marker)
    found = heapq.nlargest(limit + 1, candidates,
                           key=lambda event: (event["timestamp"], event["id"]))
    page = found[:limit]
    next_cursor = (_make_cursor(page[-1]["timestamp"], page[-1]["id"], digest)
                   if len(found) > limit else None)
    return {"events": page, "next_cursor": next_cursor}


def get_events(vault: Any, ids: Iterable[str], *,
               scope: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    chosen = list(dict.fromkeys(ids))
    if len(chosen) > MAX_IDS or any(not isinstance(value, str) or not value for value in chosen):
        raise ValueError(f"get_events accepts at most {MAX_IDS} nonempty IDs")
    selected = _scope(scope)
    if selected["projects"] == () or selected["sources"] == ():
        return []
    records = []
    for start in range(0, len(chosen), 500):
        records.extend(vault.get_records(chosen[start:start + 500]))
    return [_event(vault, record) for record in records
            if _matches(_event(vault, record, decrypt=False), selected)]


def search_events(vault: Any, query: str, *, scope: dict[str, Any] | None = None,
                  limit: int = 10) -> list[dict[str, Any]]:
    if not isinstance(query, str) or not query.strip() or len(query.encode("utf-8")) > 4096:
        raise ValueError("query must be nonempty and at most 4096 UTF-8 bytes")
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("search limit must be between 1 and 100")
    selected = _scope(scope)
    candidate_ids = [event["id"] for event in iter_events(vault, scope=selected)] if scope is not None else None
    if candidate_ids == []:
        return []
    if candidate_ids is None:
        result = vault.search(query, n_results=limit, hybrid=True)
    else:
        result = vault.search(query, n_results=limit, hybrid=True, candidate_ids=candidate_ids)
    ids = list(getattr(result, "ids", []) or [])
    scores = list(getattr(result, "scores", []) or [])
    hydrated = {event["id"]: event for event in get_events(vault, ids, scope=selected)}
    events = []
    for idx, doc_id in enumerate(ids):
        if doc_id in hydrated:
            event = hydrated[doc_id]
            if idx < len(scores):
                event["score"] = float(scores[idx])
            events.append(event)
    return events


def add_event(vault: Any, event_type: str, text: str, *, source: str | None = None,
              timestamp: datetime | str | int | float | None = None,
              project: str | None = None, metadata: dict[str, Any] | None = None,
              doc_id: str | None = None) -> str:
    if not isinstance(event_type, str) or event_type.lower() not in EVENT_TYPES:
        raise ValueError(f"event_type must be one of {', '.join(sorted(EVENT_TYPES))}")
    event_type = event_type.lower()
    source = _label(source or event_type, "source")
    project = _label(project, "project", nullable=True)
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError(f"event text must be at most {MAX_TEXT_BYTES} UTF-8 bytes")
    if metadata is not None and not isinstance(metadata, dict):
        raise TypeError("metadata must be an object")
    user_meta = dict(metadata or {})
    for name, value in (("event_type", event_type), ("source", source), ("project", project)):
        if name in user_meta and user_meta[name] != value:
            raise ValueError(f"metadata {name} conflicts with the event argument")
    if timestamp is None and "timestamp" in user_meta:
        timestamp = user_meta["timestamp"]
    occurred_at = _utc(timestamp, legacy=isinstance(timestamp, (int, float))) if timestamp is not None else None
    if doc_id is not None:
        _label(doc_id, "event ID")
        existing = get_events(vault, [doc_id])
        if existing:
            event = existing[0]
            generated = {"event_type", "source", "project", "timestamp", "captured_at", "timestamp_inferred"}
            comparable = {key: value for key, value in event["metadata"].items() if key not in generated}
            supplied = {key: value for key, value in user_meta.items() if key not in generated}
            if (event["text"], event["type"], event["source"], event["project"], comparable) != (
                    text, event_type, source, project, supplied) or (
                    occurred_at is not None and occurred_at != event["timestamp"]):
                raise ValueError("event ID already exists with different content")
            return doc_id
    else:
        doc_id = f"{EVENT_PREFIXES[event_type]}_{uuid.uuid4()}"
    captured_at = _utc(datetime.now(timezone.utc))
    user_meta.update({"event_type": event_type, "source": source, "project": project,
                      "timestamp": occurred_at or captured_at, "captured_at": captured_at,
                      "timestamp_inferred": occurred_at is None})
    try:
        encoded = json.dumps(user_meta, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("event metadata must be JSON serializable") from exc
    if len(encoded) > MAX_METADATA_BYTES:
        raise ValueError(f"event metadata must be at most {MAX_METADATA_BYTES} UTF-8 bytes")
    vault.add(documents=[text], ids=[doc_id], metadatas=[user_meta])
    return doc_id


def assign_project(vault: Any, ids: Iterable[str], project: str | None) -> int:
    project = _label(project, "project", nullable=True)
    events = get_events(vault, ids)
    changed = 0
    for event in events:
        if event["project"] != project and vault.update_metadata(event["id"], {"project": project}):
            changed += 1
    return changed


def forget_events(vault: Any, ids: Iterable[str]) -> dict[str, Any]:
    chosen = list(dict.fromkeys(ids))
    if len(chosen) > MAX_IDS or any(not isinstance(value, str) or not value for value in chosen):
        raise ValueError(f"forget_events accepts at most {MAX_IDS} nonempty IDs")
    before = {event["id"] for event in get_events(vault, chosen)}
    errors: list[str] = []
    try:
        vault.delete(chosen)
    except Exception as exc:
        errors.append(f"Derived index cleanup failed: {type(exc).__name__}: {exc}")
    remaining = {event["id"] for event in get_events(vault, chosen)}
    if remaining:
        errors.append("Canonical records remain after deletion")
    return {"canonical_deleted": len(before - remaining),
            "derived_invalidated": not errors, "cleanup_errors": errors}


def expire_events(vault: Any, *, before: datetime | str,
                  scope: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply age retention to occurrence time through the normal forget path."""
    boundary = _utc(before)
    result = {"canonical_deleted": 0, "derived_invalidated": True, "cleanup_errors": []}
    batch: list[str] = []
    for event in iter_events(vault, scope=scope):
        if event["timestamp"] >= boundary:
            continue
        batch.append(event["id"])
        if len(batch) == 500:
            part = forget_events(vault, batch)
            result["canonical_deleted"] += part["canonical_deleted"]
            result["derived_invalidated"] &= part["derived_invalidated"]
            result["cleanup_errors"].extend(part["cleanup_errors"])
            batch.clear()
    if batch:
        part = forget_events(vault, batch)
        result["canonical_deleted"] += part["canonical_deleted"]
        result["derived_invalidated"] &= part["derived_invalidated"]
        result["cleanup_errors"].extend(part["cleanup_errors"])
    return result


def should_capture(text: str, source: str, *, excluded_sources: Iterable[str] = (),
                   excluded_literals: Iterable[str] = ()) -> bool:
    """Return whether a source/text passes bounded literal capture exclusions."""
    _label(source, "source")
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    sources = tuple(excluded_sources)
    literals = tuple(excluded_literals)
    if len(sources) > 100 or len(literals) > 100:
        raise ValueError("capture exclusions allow at most 100 entries per list")
    for item in sources:
        _label(item, "excluded source")
    for item in literals:
        _label(item, "excluded literal")
    lowered = text.casefold()
    return source not in sources and not any(item.casefold() in lowered for item in literals)


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
    return {"count": len(events), "types": counts,
            "newest": events[0].get("timestamp") if events else None,
            "oldest": events[-1].get("timestamp") if events else None}


def current_context(vault: Any, limit: int = 20) -> dict[str, Any]:
    events = fetch_events(vault, limit=limit)["events"]
    return {"generated_at": _utc(datetime.now(timezone.utc)),
            "summary": summarize_events(events), "events": events}
