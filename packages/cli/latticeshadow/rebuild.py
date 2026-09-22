"""Rebuild the two CLI vector collections on a disposable SQLite copy."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timezone

from latticeshadow_db.latticedb import connect
from latticeshadow_db.routing import calculate_activation_entropy

from latticeshadow.vaults import (
    EMBEDDING_DIM,
    HOT_COLLECTION,
    HOT_INDEX_STRATEGY,
    MAIN_COLLECTION,
    embed_text,
    embedding_model_id,
    invalidate_holographic_indexes,
    vault_file_paths,
)


def _remove_sidecars(db_path: str, collections: list[str]) -> None:
    for collection in collections:
        for path in vault_file_paths(db_path, collection):
            if path != db_path and path not in (db_path + "-wal", db_path + "-shm"):
                if os.path.exists(path):
                    os.remove(path)


def rebuild_embeddings(db_path: str, master_key: str, hot_collection: str = HOT_COLLECTION) -> tuple[int, str | None]:
    """Re-embed stored documents, preserving IDs, metadata, timestamps, and a backup."""
    if not os.path.exists(db_path):
        return 0, None
    if hot_collection == MAIN_COLLECTION:
        raise ValueError("The hot collection must differ from the canonical collection.")

    directory = os.path.dirname(os.path.abspath(db_path))
    fd, staging = tempfile.mkstemp(prefix=".shadow-rebuild-", suffix=".sqlite", dir=directory)
    os.close(fd)
    os.chmod(staging, 0o600)
    collections = [MAIN_COLLECTION, hot_collection]
    swapped = False
    source = sqlite3.connect(db_path)
    try:
        initial_version = source.execute("PRAGMA data_version").fetchone()[0]
        target = sqlite3.connect(staging)
        try:
            source.backup(target)
        finally:
            target.close()

        total = 0
        for collection in collections:
            staged = sqlite3.connect(staging)
            try:
                count = staged.execute(
                    "SELECT COUNT(*) FROM vectors WHERE collection = ?", (collection,)
                ).fetchone()[0]
                columns = {
                    row[1] for row in staged.execute("PRAGMA table_info(collection_meta)")
                }
                model_row = (
                    staged.execute(
                        "SELECT embedding_model FROM collection_meta WHERE name = ?", (collection,)
                    ).fetchone()
                    if "embedding_model" in columns else None
                )
            finally:
                staged.close()
            if not count:
                continue
            vault = connect(
                db_path=staging,
                collection=collection,
                embedding_dim=EMBEDDING_DIM,
                embedding_model=model_row[0] if model_row else None,
                privacy=True,
                drosophila_hash=collection == MAIN_COLLECTION,
                experimental_index=HOT_INDEX_STRATEGY if collection == hot_collection else None,
                master_key=master_key,
            )
            with vault._store._connect() as conn:
                rows = conn.execute(
                    "SELECT id, document FROM vectors WHERE collection = ? ORDER BY id", (collection,)
                ).fetchall()
                for row_id, stored_document in rows:
                    stored_document = stored_document or ""
                    document = (
                        vault._privacy.decrypt_document(stored_document)
                        if stored_document.startswith("enc:") else stored_document
                    )
                    vector = embed_text(document)
                    if vector.numel() != EMBEDDING_DIM:
                        raise ValueError(f"Embedding model returned {vector.numel()} dimensions, expected {EMBEDDING_DIM}.")
                    encrypted = vault._privacy.encrypt(vector.reshape(1, -1)).squeeze(0)
                    entropy = calculate_activation_entropy(encrypted)
                    stored_vector = (
                        vault._store._get_drosophila_hasher(EMBEDDING_DIM).hash(encrypted)
                        if collection == MAIN_COLLECTION else encrypted
                    )
                    conn.execute(
                        "UPDATE vectors SET vector_blob = ?, entropy = ? WHERE id = ?",
                        (vault._store._vector_to_blob(stored_vector), entropy, row_id),
                    )
                conn.execute(
                    "UPDATE collection_meta SET embedding_model = ?, embedding_dim = ? WHERE name = ?",
                    (embedding_model_id(), EMBEDDING_DIM, collection),
                )
            total += len(rows)
            del vault

        if not total:
            return 0, None

        # SQLite is authoritative; sidecars are reconstructed from the new blobs.
        staged = sqlite3.connect(staging)
        try:
            staged.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            staged.close()
        _remove_sidecars(staging, collections)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(staging + suffix):
                os.remove(staging + suffix)

        if source.execute("PRAGMA data_version").fetchone()[0] != initial_version:
            raise RuntimeError("The vault changed during rebuild. Stop writers and retry.")
        backup_fd, backup = tempfile.mkstemp(
            prefix=f"{os.path.basename(db_path)}.pre-embedding-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-",
            suffix=".bak",
            dir=directory,
        )
        os.close(backup_fd)
        target = sqlite3.connect(backup)
        backup_complete = False
        try:
            source.backup(target)
            backup_complete = True
        finally:
            target.close()
            if not backup_complete:
                os.remove(backup)
        if source.execute("PRAGMA data_version").fetchone()[0] != initial_version:
            raise RuntimeError("The vault changed while its backup was made. Stop writers and retry.")
        os.chmod(backup, 0o600)
        source.close()

        _remove_sidecars(db_path, collections)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(db_path + suffix):
                os.remove(db_path + suffix)
        invalidate_holographic_indexes(directory)
        os.replace(staging, db_path)
        swapped = True
        os.chmod(db_path, 0o600)
        return total, backup
    finally:
        source.close()
        if not swapped and os.path.exists(staging):
            os.remove(staging)
        _remove_sidecars(staging, collections)
