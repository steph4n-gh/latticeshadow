"""Signed local audit log for important LatticeShadow events."""

from __future__ import annotations

import json
import os
import time
import hashlib
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


ZERO_HASH = "0" * 64


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class SignedAuditLog:
    def __init__(self, data_dir: str = "~/.latticeshadow"):
        self.data_dir = os.path.expanduser(data_dir)
        self.log_path = os.path.join(self.data_dir, ".audit_log.jsonl")
        self.key_path = os.path.join(self.data_dir, ".audit_key.pem")
        self.private_key = self._get_or_create_key()
        self.public_key = self.private_key.public_key()

    def _get_or_create_key(self) -> ed25519.Ed25519PrivateKey:
        if os.path.exists(self.key_path):
            with open(self.key_path, "rb") as handle:
                return serialization.load_pem_private_key(handle.read(), password=None)

        os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
        private_key = ed25519.Ed25519PrivateKey.generate()
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
        return private_key

    def _public_key_hex(self) -> str:
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()

    def _last_record(self) -> dict[str, Any] | None:
        if not os.path.exists(self.log_path):
            return None
        last = None
        with open(self.log_path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last = json.loads(line)
        return last

    def append(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        last = self._last_record()
        seq = int(last["seq"]) + 1 if last else 0
        prev_root = str(last["root_hash"]) if last else ZERO_HASH
        payload_hash = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
        event_core = {
            "seq": seq,
            "timestamp": time.time(),
            "event_type": event_type,
            "payload_hash": payload_hash,
            "prev_root": prev_root,
        }
        event_hash = hashlib.sha256(_stable_json(event_core).encode("utf-8")).hexdigest()
        root_hash = hashlib.sha256(f"{prev_root}:{event_hash}".encode("utf-8")).hexdigest()
        signature = self.private_key.sign(root_hash.encode("utf-8")).hex()
        record = {
            **event_core,
            "payload": payload,
            "event_hash": event_hash,
            "root_hash": root_hash,
            "signature": signature,
            "public_key": self._public_key_hex(),
        }

        os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return record

    def verify(self) -> tuple[bool, str]:
        if not os.path.exists(self.log_path):
            return True, "No audit records."

        prev_root = ZERO_HASH
        expected_seq = 0
        with open(self.log_path, "r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if int(record.get("seq", -1)) != expected_seq:
                    return False, f"Sequence mismatch at record {expected_seq}."
                if record.get("prev_root") != prev_root:
                    return False, f"Broken audit chain at record {expected_seq}."

                payload_hash = hashlib.sha256(_stable_json(record.get("payload", {})).encode("utf-8")).hexdigest()
                if record.get("payload_hash") != payload_hash:
                    return False, f"Payload hash mismatch at record {expected_seq}."

                event_core = {
                    "seq": record["seq"],
                    "timestamp": record["timestamp"],
                    "event_type": record["event_type"],
                    "payload_hash": record["payload_hash"],
                    "prev_root": record["prev_root"],
                }
                event_hash = hashlib.sha256(_stable_json(event_core).encode("utf-8")).hexdigest()
                root_hash = hashlib.sha256(f"{prev_root}:{event_hash}".encode("utf-8")).hexdigest()
                if record.get("event_hash") != event_hash or record.get("root_hash") != root_hash:
                    return False, f"Root hash mismatch at record {expected_seq}."

                public_key = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(record["public_key"]))
                try:
                    public_key.verify(bytes.fromhex(record["signature"]), root_hash.encode("utf-8"))
                except Exception:
                    return False, f"Signature mismatch at record {expected_seq}."

                prev_root = root_hash
                expected_seq += 1

        return True, f"Verified {expected_seq} audit record(s)."


def append_audit_event(event_type: str, payload: dict[str, Any], data_dir: str = "~/.latticeshadow") -> None:
    try:
        SignedAuditLog(data_dir=data_dir).append(event_type, payload)
    except Exception:
        pass
