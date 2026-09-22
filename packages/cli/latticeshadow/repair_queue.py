"""Human-approved repair proposal queue."""

from __future__ import annotations

import json
import os
import time
import hashlib
from typing import Any


VALID_STATUSES = {"pending", "approved", "rejected", "applied"}


def _queue_path(data_dir: str = "~/.latticeshadow") -> str:
    return os.path.join(os.path.expanduser(data_dir), "repair_queue.jsonl")


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _read_all(data_dir: str = "~/.latticeshadow") -> list[dict[str, Any]]:
    path = _queue_path(data_dir)
    if not os.path.exists(path):
        return []
    records = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


def _write_all(records: list[dict[str, Any]], data_dir: str = "~/.latticeshadow") -> None:
    path = _queue_path(data_dir)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    payload = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)


def create_repair_proposal(
    summary: str,
    source: str,
    command: str | None = None,
    diff_path: str | None = None,
    diff: str | None = None,
    risk: str = "medium",
    tests: list[str] | None = None,
    provenance: dict[str, Any] | None = None,
    data_dir: str = "~/.latticeshadow",
) -> dict[str, Any]:
    proposal = {
        "schema": "latticeshadow.repair_proposal.v1",
        "summary": summary,
        "source": source,
        "command": command,
        "diff_path": diff_path,
        "diff": diff,
        "risk": risk,
        "tests": tests or [],
        "provenance": provenance or {},
        "status": "pending",
        "created_at": time.time(),
    }
    proposal["id"] = hashlib.sha256(_stable_json(proposal).encode("utf-8")).hexdigest()[:16]
    records = _read_all(data_dir)
    records.append(proposal)
    _write_all(records, data_dir)
    return proposal


def list_repairs(
    status: str | None = None,
    limit: int = 20,
    data_dir: str = "~/.latticeshadow",
) -> list[dict[str, Any]]:
    records = _read_all(data_dir)
    if status:
        records = [record for record in records if record.get("status") == status]
    return list(reversed(records))[: max(1, int(limit))]


def update_repair_status(
    proposal_id: str,
    status: str,
    data_dir: str = "~/.latticeshadow",
) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {', '.join(sorted(VALID_STATUSES))}")
    records = _read_all(data_dir)
    for record in records:
        if record.get("id") == proposal_id:
            record["status"] = status
            record["updated_at"] = time.time()
            _write_all(records, data_dir)
            return record
    raise ValueError(f"Repair proposal not found: {proposal_id}")


def format_repair(record: dict[str, Any]) -> str:
    bits = [
        str(record.get("id", "")),
        f"[{record.get('status', 'pending')}]",
        f"risk={record.get('risk', 'unknown')}",
        str(record.get("summary", "")),
    ]
    return " ".join(bit for bit in bits if bit)
