"""Paired-device trust primitives for mesh sync and repair knowledge."""

from __future__ import annotations

import json
import os
import secrets
import socket
import time
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def _stable_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _without_signature(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "signature"}


class DeviceTrustStore:
    def __init__(self, data_dir: str = "~/.latticeshadow"):
        self.data_dir = os.path.expanduser(data_dir)
        self.identity_path = os.path.join(self.data_dir, ".device_identity.pem")
        self.store_path = os.path.join(self.data_dir, "trusted_devices.json")
        self.private_key = self._get_or_create_identity()
        self.public_key = self.private_key.public_key()

    def _get_or_create_identity(self) -> ed25519.Ed25519PrivateKey:
        if os.path.exists(self.identity_path):
            with open(self.identity_path, "rb") as handle:
                key_material = handle.read().strip()
            if key_material.startswith(b"-----BEGIN"):
                return serialization.load_pem_private_key(key_material, password=None)
            try:
                return ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key_material.decode("ascii")))
            except Exception:
                return ed25519.Ed25519PrivateKey.from_private_bytes(key_material)
        os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
        key = ed25519.Ed25519PrivateKey.generate()
        key_material = key.private_bytes_raw().hex().encode("ascii") + b"\n"
        fd = os.open(self.identity_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key_material)
        return key

    def public_key_hex(self) -> str:
        if hasattr(self.public_key, "public_bytes_raw"):
            return self.public_key.public_bytes_raw().hex()
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()

    def local_identity(self, node_id: str | None = None) -> dict[str, Any]:
        return {
            "node_id": node_id or socket.gethostname(),
            "public_key": self.public_key_hex(),
            "store_path": self.store_path,
        }

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self.store_path):
            return {"schema": "latticeshadow.trust_store.v1", "devices": {}, "seen_nonces": {}}
        with open(self.store_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        data.setdefault("schema", "latticeshadow.trust_store.v1")
        data.setdefault("devices", {})
        data.setdefault("seen_nonces", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
        payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
        fd = os.open(self.store_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)

    def trust_device(self, node_id: str, public_key: str, label: str | None = None) -> dict[str, Any]:
        if not node_id or not public_key:
            raise ValueError("node_id and public_key are required")
        raw_public_key = bytes.fromhex(public_key)
        if len(raw_public_key) != 32:
            raise ValueError("public_key must be a 32-byte Ed25519 public key encoded as hex")
        data = self._load()
        record = {
            "node_id": node_id,
            "public_key": public_key,
            "label": label or node_id,
            "trusted_at": time.time(),
            "revoked": False,
        }
        data["devices"][node_id] = record
        self._save(data)
        return record

    def revoke_device(self, node_id: str) -> dict[str, Any]:
        data = self._load()
        if node_id not in data["devices"]:
            raise ValueError(f"Trusted device not found: {node_id}")
        data["devices"][node_id]["revoked"] = True
        data["devices"][node_id]["revoked_at"] = time.time()
        self._save(data)
        return data["devices"][node_id]

    def list_devices(self) -> list[dict[str, Any]]:
        return list(self._load()["devices"].values())

    def sign_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        signed = dict(payload)
        signed.setdefault("signed_at", time.time())
        signed.setdefault("nonce", secrets.token_hex(16))
        signed["public_key"] = self.public_key_hex()
        signed["signature"] = self.private_key.sign(_stable_json(_without_signature(signed))).hex()
        return signed

    def verify_payload(self, payload: dict[str, Any]) -> tuple[bool, str]:
        node_id = str(payload.get("node_id") or "")
        signature = payload.get("signature")
        nonce = payload.get("nonce")
        if not node_id or not signature or not nonce:
            return False, "missing node_id, signature, or nonce"

        data = self._load()
        device = data["devices"].get(node_id)
        if not device or device.get("revoked"):
            return False, "device is not trusted"
        if payload.get("public_key") != device.get("public_key"):
            return False, "public key mismatch"

        seen = data.setdefault("seen_nonces", {}).setdefault(node_id, [])
        if nonce in seen:
            return False, "replay nonce"

        try:
            public_key = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(device["public_key"]))
            public_key.verify(bytes.fromhex(signature), _stable_json(_without_signature(payload)))
        except Exception:
            return False, "signature verification failed"

        seen.append(nonce)
        del seen[:-200]
        self._save(data)
        return True, "verified"


def trust_status(data_dir: str = "~/.latticeshadow") -> dict[str, Any]:
    """Read paired-device trust posture without creating a local identity."""
    expanded = os.path.expanduser(data_dir)
    identity_path = os.path.join(expanded, ".device_identity.pem")
    store_path = os.path.join(expanded, "trusted_devices.json")
    devices: list[dict[str, Any]] = []
    status = "missing"
    error = None

    if os.path.exists(store_path):
        try:
            with open(store_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            devices = list((data.get("devices") or {}).values())
            status = "ok"
        except Exception as exc:
            status = "error"
            error = str(exc)

    revoked_count = sum(1 for device in devices if device.get("revoked"))
    active_count = len(devices) - revoked_count
    report = {
        "schema": "latticeshadow.trust_status.v1",
        "identity_path": identity_path,
        "identity_exists": os.path.exists(identity_path),
        "store_path": store_path,
        "store_exists": os.path.exists(store_path),
        "trusted_count": active_count,
        "revoked_count": revoked_count,
        "status": status,
    }
    if error:
        report["error"] = error
    return report
