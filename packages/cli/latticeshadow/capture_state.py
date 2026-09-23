"""Capture status shared by the CLI and desktop panel."""

from __future__ import annotations

import subprocess
from typing import Any

from latticeshadow import consent


LAUNCHD_LABEL = "com.latticedb.shadow"


def get_status() -> dict[str, Any]:
    """Report observable daemon and capture state without opening the vault."""
    choice = consent.consent_status()
    running: bool | None = None
    error: str | None = None
    try:
        result = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            error = result.stderr.strip() or "launchctl status unavailable"
        else:
            running = False
            for line in result.stdout.splitlines():
                fields = line.split()
                if not fields or fields[-1] != LAUNCHD_LABEL:
                    continue
                running = len(fields) < 3 or fields[0] != "-"
                break
    except (OSError, subprocess.TimeoutExpired) as exc:
        error = str(exc)

    pending = [
        name for name in consent.CAPTURE_SOURCES
        if choice["surfaces"][name]["needs_consent"]
    ]
    sources = {
        name: {
            "enabled": choice["surfaces"][name]["enabled"],
            "needs_consent": choice["surfaces"][name]["needs_consent"],
            "capturing": bool(
                running and not choice["paused"]
                and choice["surfaces"][name]["enabled"]
                and not choice["surfaces"][name]["needs_consent"]
            ),
        }
        for name in consent.CAPTURE_SOURCES
    }

    if pending:
        state = "consent_needed"
    elif running is None:
        state = "unavailable"
    elif not running:
        state = "stopped"
    elif choice["paused"]:
        state = "paused"
    elif any(item["capturing"] for item in sources.values()):
        state = "capturing"
    else:
        state = "idle"

    return {
        "state": state,
        "daemon_running": running,
        "paused": choice["paused"],
        "consent_needed": pending,
        "sources": sources,
        "error": error,
    }
