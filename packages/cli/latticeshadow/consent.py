"""Consent state helpers for listener and sync surfaces."""

from __future__ import annotations

from typing import Any, Callable

from latticeshadow import config


SURFACES: dict[str, dict[str, Any]] = {
    "clipboard": {
        "config_key": "inputs.clipboard",
        "label": "Clipboard capture",
        "boundary": "Stores copied text in the local encrypted vault.",
    },
    "terminal_history": {
        "config_key": "inputs.terminal_history",
        "label": "Terminal history capture",
        "boundary": "Stores shell commands from local history files.",
    },
    "ambient_context": {
        "config_key": "inputs.ambient_context",
        "label": "Ambient app context",
        "boundary": "May inspect active app/window context when enabled.",
    },
    "mobile_api": {
        "config_key": "mobile.enabled",
        "label": "Mobile API",
        "boundary": "Starts a localhost API for paired clients.",
    },
    "icloud_sync": {
        "config_key": "sync.icloud_sync",
        "label": "iCloud sync",
        "boundary": "Writes encrypted sync packets to the user's iCloud folder.",
    },
    "mesh_sync": {
        "config_key": "sync.mesh_sync",
        "label": "P2P mesh sync",
        "boundary": "Advertises and exchanges sync messages on the local network.",
    },
    "swarm_knowledge": {
        "config_key": "sync.swarm_knowledge",
        "label": "Swarm repair knowledge",
        "boundary": "Records signed repair proposals from trusted peers.",
    },
    "hot_index": {
        "config_key": "memory.hot_index_enabled",
        "label": "Streaming exact hot index",
        "boundary": "Mirrors captures into an opt-in dense sidecar collection.",
    },
}

CAPTURE_SOURCES = ("clipboard", "terminal_history")


def _set_nested(root: dict[str, Any], dotted_key: str, value: Any) -> None:
    current = root
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _get_nested(root: dict[str, Any], dotted_key: str) -> Any:
    current = root
    for part in dotted_key.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def consent_status() -> dict[str, Any]:
    cfg = config.load_config()
    consent_cfg = cfg.get("consent", {})
    surfaces_cfg = consent_cfg.get("surfaces", {})
    surfaces = {}
    for name, spec in SURFACES.items():
        enabled = bool(_get_nested(cfg, spec["config_key"]))
        consented = surfaces_cfg.get(name)
        surfaces[name] = {
            "label": spec["label"],
            "boundary": spec["boundary"],
            "config_key": spec["config_key"],
            "enabled": enabled,
            "consented": bool(consented) if consented is not None else False,
            "needs_consent": not isinstance(consented, bool) or consented != enabled,
        }
    return {
        "completed": bool(consent_cfg.get("completed")),
        "version": int(consent_cfg.get("version", 1)),
        "paused": bool(cfg.get("inputs", {}).get("paused", False)),
        "surfaces": surfaces,
    }


def pending_capture_sources() -> list[str]:
    surfaces = consent_status()["surfaces"]
    return [name for name in CAPTURE_SOURCES if surfaces[name]["needs_consent"]]


def capture_enabled(name: str) -> bool:
    if name not in CAPTURE_SOURCES:
        raise ValueError(f"Unknown capture source: {name}")
    status = consent_status()
    state = status["surfaces"][name]
    return state["enabled"] and not state["needs_consent"] and not status["paused"]


def set_paused(paused: bool) -> dict[str, Any]:
    """Persist the capture pause without changing source choices or consent."""
    if not isinstance(paused, bool):
        raise TypeError("paused must be a bool")
    if not paused:
        pending = pending_capture_sources()
        if pending:
            raise ValueError("Choose capture sources before resuming: " + ", ".join(pending))
    cfg = config.load_config()
    cfg.setdefault("inputs", {})["paused"] = paused
    config.save_config(cfg)
    return consent_status()


def set_consent(surface: str, enabled: bool) -> dict[str, Any]:
    if surface not in SURFACES:
        raise ValueError(f"Unknown consent surface: {surface}")

    cfg = config.load_config()
    consent_cfg = cfg.setdefault("consent", {})
    consent_cfg.setdefault("version", 1)
    surfaces_cfg = consent_cfg.setdefault("surfaces", {})
    surfaces_cfg[surface] = bool(enabled)
    _set_nested(cfg, SURFACES[surface]["config_key"], bool(enabled))
    config.save_config(cfg)
    return consent_status()


def mark_completed() -> dict[str, Any]:
    cfg = config.load_config()
    consent_cfg = cfg.setdefault("consent", {})
    consent_cfg["version"] = 1
    consent_cfg["completed"] = True
    consent_cfg.setdefault("surfaces", {})
    config.save_config(cfg)
    return consent_status()


def run_wizard(
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> dict[str, Any]:
    output_fn("LatticeShadow consent wizard")
    output_fn("Each surface can be changed later with 'shadow consent set <surface> on|off'.")
    for name, spec in SURFACES.items():
        default = "y" if bool(config.get(spec["config_key"])) else "n"
        answer = input_fn(f"{spec['label']}? {spec['boundary']} [{default}/{'n' if default == 'y' else 'y'}]: ")
        answer = answer.strip().lower() or default
        set_consent(name, answer in ("y", "yes", "on", "true", "1"))
    return mark_completed()
