"""Passphrase-encrypted logical backup for a new, empty vault destination.

The 256 MiB plaintext cap still needs several times that much process memory:
SQLite rows, JSON encoding, and authenticated encryption coexist briefly.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from latticeshadow.timeline import _utc, MAX_TEXT_BYTES
from latticeshadow.vaults import MAIN_COLLECTION, open_main_vault, vault_file_paths

MAGIC = b"LSBK1"
SALT_BYTES = 16
NONCE_BYTES = 12
MAX_PLAINTEXT_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_PLAINTEXT_BYTES + 64
MAX_RECORDS = 100_000


def _key(passphrase: str, salt: bytes) -> bytes:
    if not isinstance(passphrase, str) or not 8 <= len(passphrase) <= 4096:
        raise ValueError("Backup passphrase must contain 8 to 4096 characters")
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode("utf-8"))


def _snapshot(vault: Any) -> dict[str, Any]:
    """Read one consistent canonical SQLite snapshot and decrypt every record."""
    if vault.name != MAIN_COLLECTION:
        raise ValueError("Portable backup requires the canonical clipboard collection")
    conn = sqlite3.connect(vault.db_path, timeout=30)
    try:
        conn.execute("BEGIN")
        model_row = conn.execute(
            "SELECT embedding_model, embedding_dim FROM collection_meta WHERE name = ?",
            (vault.name,),
        ).fetchone()
        records = []
        used = 0
        for doc_id, document, metadata_json, created_at, last_accessed in conn.execute(
            "SELECT doc_id, document, metadata_json, created_at, last_accessed "
            "FROM vectors WHERE collection = ? ORDER BY id", (vault.name,)
        ):
            document = document or ""
            if document.startswith("enc:"):
                try:
                    document = vault._privacy.decrypt_document(document)
                except Exception as exc:
                    raise ValueError(f"Cannot decrypt source event {doc_id}") from exc
            if len(document.encode("utf-8")) > MAX_TEXT_BYTES:
                raise ValueError(f"Source event {doc_id} exceeds the event text limit")
            metadata = json.loads(metadata_json or "{}")
            if not isinstance(metadata, dict):
                raise ValueError(f"Source event {doc_id} has invalid metadata")
            _utc(created_at, legacy=True)
            _utc(last_accessed, legacy=True)
            item = {"id": doc_id, "text": document, "metadata": metadata,
                    "created_at": created_at, "last_accessed": last_accessed}
            records.append(item)
            used += len(document.encode("utf-8")) + len((metadata_json or "").encode("utf-8")) + len(doc_id)
            if len(records) > MAX_RECORDS or used > MAX_PLAINTEXT_BYTES:
                raise ValueError("Vault exceeds the portable backup size limit")
        try:
            tombstones = []
            for (doc_id,) in conn.execute(
                "SELECT doc_id FROM deleted_ids WHERE collection = ? ORDER BY doc_id", (vault.name,)
            ):
                tombstones.append(doc_id)
                used += len(doc_id.encode("utf-8"))
                if len(records) + len(tombstones) > MAX_RECORDS or used > MAX_PLAINTEXT_BYTES:
                    raise ValueError("Vault exceeds the portable backup size limit")
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            tombstones = []
        conn.commit()
        return {"version": 1, "collection": vault.name,
                "source_model": model_row[0] if model_row else None,
                "source_dim": model_row[1] if model_row else None,
                "records": records, "deleted_ids": tombstones}
    finally:
        conn.close()


def export_backup(vault: Any, archive_path: str | os.PathLike[str], passphrase: str) -> dict[str, Any]:
    """Write a private authenticated archive; never create a plaintext temp file."""
    destination = Path(archive_path)
    if destination.exists():
        raise FileExistsError(f"Backup already exists: {destination}")
    snapshot = _snapshot(vault)
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_PLAINTEXT_BYTES:
        raise ValueError("Vault exceeds the 256 MiB portable backup limit")
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    header = MAGIC + salt + nonce
    ciphertext = AESGCM(_key(passphrase, salt)).encrypt(nonce, payload, header)
    fd, temporary = tempfile.mkstemp(prefix=".shadow-backup-", dir=destination.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(header)
            output.write(ciphertext)
            output.flush()
            os.fsync(output.fileno())
        if destination.exists():
            raise FileExistsError(f"Backup already exists: {destination}")
        os.link(temporary, destination)
        return {"records": len(snapshot["records"]), "deleted_ids": len(snapshot["deleted_ids"]),
                "bytes": destination.stat().st_size, "source_model": snapshot["source_model"]}
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_archive(path: str | os.PathLike[str], passphrase: str) -> dict[str, Any]:
    archive = Path(path)
    size = archive.stat().st_size
    if size < len(MAGIC) + SALT_BYTES + NONCE_BYTES + 16 or size > MAX_ARCHIVE_BYTES:
        raise ValueError("Backup has an invalid size")
    data = archive.read_bytes()
    header_len = len(MAGIC) + SALT_BYTES + NONCE_BYTES
    header = data[:header_len]
    if not header.startswith(MAGIC):
        raise ValueError("Unsupported backup format")
    salt = header[len(MAGIC):len(MAGIC) + SALT_BYTES]
    nonce = header[-NONCE_BYTES:]
    try:
        plaintext = AESGCM(_key(passphrase, salt)).decrypt(nonce, data[header_len:], header)
    except Exception as exc:
        raise ValueError("Backup authentication failed; check the passphrase and archive") from exc
    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise ValueError("Backup exceeds the 256 MiB plaintext limit")
    try:
        payload = json.loads(plaintext)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Backup payload is invalid JSON") from exc
    _validate(payload)
    return payload


def _validate(payload: Any) -> None:
    if not isinstance(payload, dict) or set(payload) != {
            "version", "collection", "source_model", "source_dim", "records", "deleted_ids"}:
        raise ValueError("Backup schema is invalid")
    if payload["version"] != 1 or payload["collection"] != MAIN_COLLECTION:
        raise ValueError("Unsupported backup version or collection")
    if payload["source_model"] is not None and not isinstance(payload["source_model"], str):
        raise ValueError("Backup model identity is invalid")
    if payload["source_dim"] is not None and (not isinstance(payload["source_dim"], int) or payload["source_dim"] <= 0):
        raise ValueError("Backup model dimension is invalid")
    records = payload["records"]
    deleted = payload["deleted_ids"]
    if not isinstance(records, list) or not isinstance(deleted, list) or len(records) + len(deleted) > MAX_RECORDS:
        raise ValueError("Backup record count is invalid")
    ids = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"id", "text", "metadata", "created_at", "last_accessed"}:
            raise ValueError("Backup record schema is invalid")
        doc_id = record["id"]
        if not isinstance(doc_id, str) or not doc_id or len(doc_id.encode("utf-8")) > 1024 or doc_id in ids:
            raise ValueError("Backup contains a duplicate or invalid event ID")
        ids.add(doc_id)
        if not isinstance(record["text"], str) or len(record["text"].encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("Backup event text is invalid")
        if not isinstance(record["metadata"], dict):
            raise ValueError("Backup metadata is invalid")
        if len(json.dumps(record["metadata"]).encode("utf-8")) > 64 * 1024:
            raise ValueError("Backup metadata exceeds the event limit")
        _utc(record["created_at"], legacy=True)
        _utc(record["last_accessed"], legacy=True)
    for doc_id in deleted:
        if not isinstance(doc_id, str) or not doc_id or len(doc_id.encode("utf-8")) > 1024 or doc_id in ids:
            raise ValueError("Backup contains a duplicate or invalid deleted ID")
        ids.add(doc_id)


def restore_backup(archive_path: str | os.PathLike[str], destination_db_path: str | os.PathLike[str],
                   passphrase: str, destination_master_key: str) -> dict[str, Any]:
    """Restore into a new private vault without touching Keychain or capture settings."""
    payload = _read_archive(archive_path, passphrase)
    destination = Path(destination_db_path)
    if destination.exists():
        raise FileExistsError(f"Restore destination already exists: {destination}")
    if not isinstance(destination_master_key, str) or not destination_master_key:
        raise ValueError("An explicit destination master key is required")
    fd, staging = tempfile.mkstemp(prefix=".shadow-restore-", suffix=".sqlite", dir=destination.parent)
    os.close(fd)
    os.chmod(staging, 0o600)
    try:
        vault = open_main_vault(staging, destination_master_key, device="cpu")
        records = payload["records"]
        for start in range(0, len(records), 64):
            batch = records[start:start + 64]
            vault.add(documents=[record["text"] for record in batch],
                      ids=[record["id"] for record in batch],
                      metadatas=[record["metadata"] for record in batch])
        with sqlite3.connect(staging) as conn:
            conn.executemany(
                "UPDATE vectors SET created_at = ?, last_accessed = ? WHERE collection = ? AND doc_id = ?",
                [(record["created_at"], record["last_accessed"], MAIN_COLLECTION, record["id"])
                 for record in records],
            )
        with sqlite3.connect(staging) as conn:
            conn.executemany("INSERT INTO deleted_ids (collection, doc_id) VALUES (?, ?)",
                             [(MAIN_COLLECTION, doc_id) for doc_id in payload["deleted_ids"]])
        with sqlite3.connect(staging) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        del vault
        for path in vault_file_paths(staging):
            if path != staging:
                Path(path).unlink(missing_ok=True)
        # hardlink fails if another process created the destination while we worked.
        os.link(staging, destination)
        return {"records": len(payload["records"]), "deleted_ids": len(payload["deleted_ids"]),
                "source_model": payload["source_model"], "destination": str(destination)}
    finally:
        Path(staging).unlink(missing_ok=True)
        for path in vault_file_paths(staging):
            if path != staging:
                Path(path).unlink(missing_ok=True)
