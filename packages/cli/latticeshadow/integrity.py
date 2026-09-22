"""
Runtime Self-Integrity Verification for LatticeShadow.

On first daemon start, computes SHA-256 hashes of all critical source modules
and writes them to ~/.latticeshadow/.integrity_manifest.

On subsequent starts, re-computes hashes and compares against the manifest.
If any file has been modified (e.g. by malware injection), the daemon logs
a CRITICAL warning and refuses to start.

This protects against post-install tampering of the daemon's own code.
"""

import hashlib
import json
import os
import logging

logger = logging.getLogger("shadowd")

# Modules whose integrity we verify on every daemon start
CRITICAL_MODULES = [
    "shadowd.py",
    "shadow_cli.py",
    "config.py",
    "keychain.py",
    "p2p.py",
    "mobile_api.py",
    "ambient_monitor.py",
    "history_watcher.py",
    "tda.py",
    "menu.py",
]


def _sha256_file(path: str) -> str:
    """Compute the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
    except (OSError, IOError):
        return ""
    return h.hexdigest()


def _get_manifest_path() -> str:
    return os.path.join(os.path.expanduser("~/.latticeshadow"), ".integrity_manifest")


def _get_module_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def compute_manifest() -> dict[str, str]:
    """Compute SHA-256 hashes for all critical modules."""
    module_dir = _get_module_dir()
    manifest = {}
    for mod in CRITICAL_MODULES:
        path = os.path.join(module_dir, mod)
        if os.path.exists(path):
            manifest[mod] = _sha256_file(path)
    return manifest


def write_manifest(manifest: dict[str, str]) -> None:
    """Write the integrity manifest to disk with owner-only permissions."""
    manifest_path = _get_manifest_path()
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(manifest, f, indent=2)


def read_manifest() -> dict[str, str] | None:
    """Read the stored integrity manifest, or None if it doesn't exist."""
    manifest_path = _get_manifest_path()
    if not os.path.exists(manifest_path):
        return None
    try:
        with open(manifest_path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def verify_integrity() -> tuple[bool, list[str]]:
    """
    Verify the daemon's source code integrity.

    Returns:
        (passed, violations) where violations is a list of human-readable
        descriptions of what changed.

    On first run (no manifest exists), creates the baseline manifest and
    returns (True, []).
    """
    current = compute_manifest()
    stored = read_manifest()

    if stored is None:
        # First run — establish the baseline
        write_manifest(current)
        logger.info("Integrity manifest created (%d modules baselined)", len(current))
        return True, []

    violations = []

    # Check for modified files
    for mod, expected_hash in stored.items():
        actual_hash = current.get(mod, "")
        if actual_hash == "":
            violations.append(f"MISSING: {mod} (expected hash {expected_hash[:16]}...)")
        elif actual_hash != expected_hash:
            violations.append(
                f"MODIFIED: {mod} "
                f"(expected {expected_hash[:16]}..., got {actual_hash[:16]}...)"
            )

    # Check for new unexpected files injected into the module directory
    for mod in current:
        if mod not in stored:
            violations.append(f"NEW FILE: {mod} (not in original manifest)")

    if violations:
        return False, violations

    return True, []
