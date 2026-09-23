"""Local allowlists for the read-only assistant bridge."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from latticeshadow.timeline import fetch_events, iter_events

MAX_GRANT_BYTES = 1_048_576
MAX_RESULTS = 50


def _utc(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("grant time must be an ISO 8601 string with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("grant time must be an ISO 8601 string with a timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("grant time must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _names(values: Any, name: str, *, unassigned: bool = False) -> list[str | None]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 100:
        raise ValueError(f"{name} must be an explicit nonempty allowlist of at most 100 entries")
    result = []
    for value in values:
        if value is None and unassigned:
            pass
        elif not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
            raise ValueError(f"{name} entries must be nonempty strings of at most 256 bytes")
        if value not in result:
            result.append(value)
    return result


def validate_grant(grant: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(grant, dict) or set(grant) != {"id", "revision", "projects", "sources", "since", "until", "limit", "created_at"}:
        raise ValueError("invalid sharing grant")
    if not isinstance(grant["id"], str) or not grant["id"].startswith("grant_") or len(grant["id"]) > 80:
        raise ValueError("invalid sharing grant ID")
    if not isinstance(grant["revision"], str) or len(grant["revision"]) > 80:
        raise ValueError("invalid sharing grant revision")
    projects = _names(grant["projects"], "projects", unassigned=True)
    sources = _names(grant["sources"], "sources")
    since, until = _utc(grant["since"]), _utc(grant["until"])
    if since and until and since >= until:
        raise ValueError("grant since must be earlier than until")
    limit = grant["limit"]
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f"grant result cap must be between 1 and {MAX_RESULTS}")
    created_at = _utc(grant["created_at"])
    if created_at is None:
        raise ValueError("grant creation time is required")
    return {"id": grant["id"], "revision": grant["revision"], "projects": projects,
            "sources": sources, "since": since, "until": until, "limit": limit,
            "created_at": created_at}


def _path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / "sharing_grants.json"


@contextmanager
def _mutation_lock(data_dir: str | os.PathLike[str]):
    """Keep read/modify/write grant changes ordered across CLI processes."""
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / "sharing_grants.lock",
                 os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(data_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    path = _path(data_dir)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return []
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(MAX_GRANT_BYTES + 1)
    if len(raw) > MAX_GRANT_BYTES:
        raise ValueError("sharing grant store is too large")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"grants"} or not isinstance(value["grants"], list):
        raise ValueError("invalid sharing grant store")
    grants = [validate_grant(item) for item in value["grants"]]
    if len({item["id"] for item in grants}) != len(grants):
        raise ValueError("duplicate sharing grant ID")
    return grants


def _write(data_dir: str | os.PathLike[str], grants: list[dict[str, Any]]) -> None:
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = json.dumps({"grants": grants}, separators=(",", ":"), sort_keys=True).encode()
    if len(body) > MAX_GRANT_BYTES:
        raise ValueError("sharing grant store is too large")
    fd, temporary = tempfile.mkstemp(prefix=".sharing-grants-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def create_grant(data_dir: str | os.PathLike[str], *, projects: list[str | None],
                 sources: list[str], since: str | None = None, until: str | None = None,
                 limit: int = 20) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    grant = validate_grant({"id": f"grant_{uuid.uuid4().hex}", "revision": uuid.uuid4().hex,
                            "projects": projects, "sources": sources, "since": since,
                            "until": until, "limit": limit, "created_at": now})
    with _mutation_lock(data_dir):
        grants = _read(data_dir)
        _write(data_dir, [*grants, grant])
    return grant


def list_grants(data_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    return _read(data_dir)


def load_grant(data_dir: str | os.PathLike[str], grant_id: str) -> dict[str, Any] | None:
    return next((grant for grant in _read(data_dir) if grant["id"] == grant_id), None)


def revoke_grant(data_dir: str | os.PathLike[str], grant_id: str) -> bool:
    with _mutation_lock(data_dir):
        grants = _read(data_dir)
        remaining = [grant for grant in grants if grant["id"] != grant_id]
        if len(remaining) == len(grants):
            return False
        _write(data_dir, remaining)
    from latticeshadow import timeline
    clear = getattr(timeline, "clear_search_cache", None)
    if clear is not None:
        clear()
    return True


def grant_scope(grant: dict[str, Any]) -> dict[str, Any]:
    checked = validate_grant(grant)
    return {key: checked[key] for key in ("projects", "sources", "since", "until")}


def intersect_grants(ceiling: dict[str, Any], current: dict[str, Any],
                     requested: dict[str, Any] | None = None) -> tuple[dict[str, Any], int]:
    """Apply a request to both immutable startup ceiling and current local grant."""
    ceiling, current = validate_grant(ceiling), validate_grant(current)
    if ceiling["id"] != current["id"]:
        raise ValueError("sharing grant unavailable")
    if requested is None:
        requested = {}
    if not isinstance(requested, dict) or set(requested) - {"projects", "sources", "since", "until"}:
        raise ValueError("scope accepts projects, sources, since and until")
    selected: dict[str, Any] = {}
    for field in ("projects", "sources"):
        subset = requested.get(field)
        if subset is not None:
            if not isinstance(subset, (list, tuple)) or len(subset) > 100:
                raise ValueError(f"{field} must be a list of at most 100 entries")
            if subset:
                subset = _names(subset, field, unassigned=field == "projects")
        selected[field] = [item for item in ceiling[field]
                           if item in current[field] and (subset is None or item in subset)]
    for field in ("since", "until"):
        bounds = [value for value in (ceiling[field], current[field], _utc(requested.get(field)))
                  if value is not None]
        selected[field] = (max(bounds) if field == "since" else min(bounds)) if bounds else None
    if selected["since"] and selected["until"] and selected["since"] >= selected["until"]:
        selected["projects"] = []
    return selected, min(ceiling["limit"], current["limit"])


def preview_grant(vault: Any, grant: dict[str, Any], *, limit: int = 20) -> dict[str, Any]:
    checked = validate_grant(grant)
    if type(limit) is not int or limit < 1:
        raise ValueError("preview limit must be positive")
    scope = grant_scope(checked)
    count = sum(1 for _ in iter_events(vault, scope=scope))
    page = fetch_events(vault, scope=scope, limit=min(limit, checked["limit"]))
    return {"count": count, "events": page["events"], "limit": checked["limit"]}
