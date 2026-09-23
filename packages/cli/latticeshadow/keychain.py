"""
macOS Keychain helper for LatticeShadow master key storage.

Uses /usr/bin/security CLI — zero pip dependencies required.
The master key is stored as a generic-password item in the user's
default (login) keychain, which is hardware-backed on Apple Silicon
via the Secure Enclave.
"""

import subprocess
import platform

SERVICE_NAME = "com.latticedb.shadow"
ACCOUNT_NAME = "latticeshadow-master-key"
_SECURITY = "/usr/bin/security"
_SECURITY_TIMEOUT = 10


class KeychainError(Exception):
    """Raised when a Keychain operation fails."""
    pass


class KeychainLocked(KeychainError):
    """Raised when the Keychain is locked and user interaction is not possible."""
    pass


def _check_platform():
    if platform.system() != "Darwin":
        raise KeychainError("macOS Keychain is only available on macOS (Darwin)")


def _run_security(args):
    try:
        return subprocess.run(
            [_SECURITY, *args], capture_output=True, text=True,
            timeout=_SECURITY_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise KeychainLocked(
            "Keychain access timed out. Unlock your macOS login keychain and try again."
        ) from exc


def store_key(key: str) -> None:
    """
    Store (or update) the master key in the macOS Keychain.

    Uses `security add-generic-password -U` for idempotent upsert.
    """
    _check_platform()
    result = _run_security(
        [
            "add-generic-password",
            "-a", ACCOUNT_NAME,
            "-s", SERVICE_NAME,
            "-w", key,
            "-U",  # update if exists
        ],
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "errSecInteractionNotAllowed" in stderr or "-25308" in stderr:
            raise KeychainLocked(
                "Keychain is locked. Unlock your keychain (e.g., open Keychain Access) and try again."
            )
        raise KeychainError(f"Failed to store key in Keychain: {stderr}")


def retrieve_key() -> str | None:
    """
    Retrieve the master key from the macOS Keychain.

    Returns the key string, or None if not found.
    """
    _check_platform()
    result = _run_security(
        [
            "find-generic-password",
            "-a", ACCOUNT_NAME,
            "-s", SERVICE_NAME,
            "-w",  # output only the password
        ],
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "could not be found" in stderr or "errSecItemNotFound" in stderr or "-25300" in stderr:
            return None
        if "errSecInteractionNotAllowed" in stderr or "-25308" in stderr:
            raise KeychainLocked(
                "Keychain is locked. Unlock your keychain (e.g., open Keychain Access) and try again."
            )
        raise KeychainError(f"Failed to retrieve key from Keychain: {stderr}")
    return result.stdout.strip()


def delete_key() -> bool:
    """
    Delete the master key from the macOS Keychain.

    Returns True if deleted, False if the item was not found.
    """
    _check_platform()
    result = _run_security(
        [
            "delete-generic-password",
            "-a", ACCOUNT_NAME,
            "-s", SERVICE_NAME,
        ],
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "could not be found" in stderr or "errSecItemNotFound" in stderr or "-25300" in stderr:
            return False
        raise KeychainError(f"Failed to delete key from Keychain: {stderr}")
    return True
