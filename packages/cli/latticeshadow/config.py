"""
LatticeShadow configuration system.

Reads/writes ~/.latticeshadow/config.toml with sensible defaults.
Uses Python 3.11+ tomllib for reading (stdlib, zero deps).
Writes TOML manually (no tomli-w dependency needed for flat configs).
"""

import os
import sys
import copy
import json
import tempfile
import fcntl
from contextlib import contextmanager

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore

LOG_DIR = os.path.expanduser("~/.latticeshadow")
CONFIG_PATH = os.path.join(LOG_DIR, "config.toml")

DEFAULTS = {
    "memory": {
        "provider": "none",            # "gemini" | "openai" | "ollama" | "lmstudio" | "none"
        "model": "",                   # model name (provider-specific)
        "dream_batch_size": 50,        # clips per LLM call during REM Sleep
        "sensitivity_filter": True,    # skip secrets before sending to API
        "hot_index_enabled": False,     # opt-in dense streaming-exact sidecar
        "hot_index_strategy": "streaming_exact",
        "hot_index_collection": "clipboard_hot",
        "endpoints": {
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "openai": "https://api.openai.com/v1/",
            "ollama": "http://localhost:11434/v1/",
            "lmstudio": "http://localhost:1234/v1/",
        },
    },
    "inputs": {
        "clipboard": False,
        "terminal_history": False,
        "ambient_context": False,
        "paused": False,
        "terminal_history_epoch": 0,
        "clipboard_epoch": 0,
        "excluded_sources": [],
        "excluded_literals": [],
    },
    "retention": {
        "days": 0,                  # zero disables automatic age-based deletion
    },
    "sync": {
        "icloud_sync": False,
        "mesh_sync": False,
        "swarm_knowledge": False,
        "mesh_trust_required": True,
    },
    "mobile": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 5052,
    },
    "time_travel": {
        "enabled": False,
        "include_environment": False,
    },
    "hardware": {
        "device": "auto",
    },
    "automation": {
        "auto_doctor_enabled": False,
        "test_command": "pytest",
        "idle_threshold_seconds": 300,
        "max_daily_llm_requests": 50,
    },
    "experimental": {
        "immune_scan": False,
        "semantic_swapper": False,
    },
    "consent": {
        "completed": False,
        "version": 1,
        "surfaces": {},
    },
}

# Default models per provider (used when user sets provider but not model)
DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
    "ollama": "llama3.2",
    "lmstudio": "default",
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict:
    """
    Load config from ~/.latticeshadow/config.toml, merged with defaults.
    Returns defaults if the file doesn't exist.
    """
    config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "rb") as f:
            config = tomllib.load(f)
    return _deep_merge(DEFAULTS, config)


@contextmanager
def mutation_lock():
    """Serialize config read/modify/write across the CLI, menu and daemon."""
    os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(LOG_DIR, "config.lock"),
                 os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def get_data_dir() -> str:
    """Keep live vault data local, including when encrypted iCloud sync is enabled."""
    icloud_dir = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/LatticeShadow")
    if get("sync.icloud_sync") and os.path.isdir(icloud_dir):
        legacy_names = {
            ".key", "shadow.sqlite", "shadow.sqlite-wal", "shadow.sqlite-shm",
            "shadowd.log", "launchd.out", "launchd.err", ".ambient_context",
            ".speculative_fix", "repair_queue.jsonl", ".audit_key.pem",
            ".audit_log.jsonl", ".device_identity.pem", "trusted_devices.json",
            ".pot_chain.jsonl", ".pot_key.pem", "snapshots",
        }
        with os.scandir(icloud_dir) as entries:
            if any(
                entry.name in legacy_names
                or entry.name.startswith(("shadow.sqlite_", "holographic_", "shadowd.log."))
                for entry in entries
            ):
                raise RuntimeError(
                    "Legacy live LatticeShadow data is in the iCloud LatticeShadow folder. "
                    "Stop the daemon, back up both folders, and copy the live vault and "
                    "auxiliary files to ~/.latticeshadow without overwriting local data. "
                    "Temporarily disable sync.icloud_sync to verify local recall before "
                    "removing old iCloud copies; then re-enable encrypted packet sync."
                )
    return LOG_DIR


def save_config(config: dict) -> None:
    """Atomically write config so a crash cannot truncate capture choices."""
    os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
    lines = []
    _write_toml(config, lines, depth=0)
    payload = "\n".join(lines) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=LOG_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, CONFIG_PATH)
        try:
            directory_fd = os.open(LOG_DIR, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_toml(data: dict, lines: list, depth: int, prefix: str = "") -> None:
    """Serialize a dict to TOML lines. Handles one level of nesting."""
    # Write scalar values first
    for key, value in data.items():
        if isinstance(value, dict):
            continue
        full_key = f"{prefix}{key}" if not prefix else key
        lines.append(f"{key} = {_toml_value(value)}")

    # Then write table sections
    for key, value in data.items():
        if isinstance(value, dict):
            section = f"{prefix}{key}" if prefix else key
            lines.append(f"\n[{section}]")
            _write_toml(value, lines, depth + 1, prefix=f"{section}.")


def _toml_value(value) -> str:
    """Convert a Python value to a TOML-compatible string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    elif isinstance(value, int):
        return str(value)
    elif isinstance(value, float):
        return str(value)
    elif isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    elif isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    else:
        raise TypeError(f"Unsupported TOML value: {type(value).__name__}")


def get(key: str) -> str | int | bool | None:
    """
    Get a config value using dot notation.
    Example: get("memory.provider") → "gemini"
    """
    config = load_config()
    parts = key.split(".")
    current = config
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def set(key: str, value: str) -> None:
    """
    Set a config value using dot notation and save.
    Example: set("memory.provider", "gemini")
    Automatically sets default model when provider changes.
    """
    with mutation_lock():
        _set_locked(key, value)


def _set_locked(key: str, value: str) -> None:
    config = load_config()
    parts = key.split(".")

    # Navigate/create nested structure
    current = config
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]

    # Type coercion based on defaults
    final_key = parts[-1]
    default_val = _get_default(key)
    if key in ("inputs.excluded_sources", "inputs.excluded_literals"):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be a JSON array of strings") from exc
        if (not isinstance(parsed, list) or len(parsed) > 100 or
                any(not isinstance(item, str) or not item or
                    len(item.encode("utf-8")) > 256 for item in parsed)):
            raise ValueError(f"{key} must contain at most 100 nonempty strings, each at most 256 bytes")
        current[final_key] = parsed
    elif key == "retention.days":
        try:
            days = int(value)
        except ValueError as exc:
            raise ValueError("retention.days must be a nonnegative integer") from exc
        if not 0 <= days <= 36500:
            raise ValueError("retention.days must be between 0 and 36500")
        current[final_key] = days
    elif isinstance(default_val, bool):
        current[final_key] = value.lower() in ("true", "1", "yes")
    elif isinstance(default_val, int):
        try:
            current[final_key] = int(value)
        except ValueError:
            current[final_key] = value
    else:
        current[final_key] = value

    # Low-level config writes are also used by migrations and tests. Keep
    # capture transitions visible to a daemon even if off/on falls between polls.
    if key in {"inputs.clipboard", "inputs.terminal_history", "inputs.paused"}:
        inputs = config.setdefault("inputs", {})
        if key in {"inputs.clipboard", "inputs.paused"}:
            inputs["clipboard_epoch"] = int(inputs.get("clipboard_epoch", 0)) + 1
        if key in {"inputs.terminal_history", "inputs.paused"}:
            inputs["terminal_history_epoch"] = int(inputs.get("terminal_history_epoch", 0)) + 1

    # Auto-set default model when provider changes
    if key == "memory.provider" and value in DEFAULT_MODELS:
        model_key = "model"
        mem = config.get("memory", {})
        if not mem.get("model"):
            mem["model"] = DEFAULT_MODELS[value]

    save_config(config)


def _get_default(key: str):
    """Look up the default value for a dot-notation key."""
    parts = key.split(".")
    current = DEFAULTS
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return ""
    return current


def format_config() -> str:
    """Return a human-readable string of the current config."""
    config = load_config()
    lines = []
    _format_dict(config, lines, indent=0)
    return "\n".join(lines)


def _format_dict(d: dict, lines: list, indent: int) -> None:
    """Recursively format a dict for display."""
    for key, value in d.items():
        if isinstance(value, dict):
            lines.append(f"{'  ' * indent}{key}:")
            _format_dict(value, lines, indent + 1)
        else:
            lines.append(f"{'  ' * indent}{key} = {value}")


def get_device() -> str:
    """Return the configured hardware device, resolving 'auto' to 'mps' if available."""
    dev = get("hardware.device") or "auto"
    if dev == "auto":
        try:
            import torch
            if hasattr(torch, "backends") and hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        return "cpu"
    return dev
