"""Portable recovery succeeds without the source key and fails without mutation."""
import sqlite3

import pytest

from latticeshadow.backup import export_backup, restore_backup
from latticeshadow.timeline import add_event, fetch_events, forget_events, get_events
from latticeshadow.vaults import open_main_vault


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setenv("LATTICESHADOW_EMBEDDING_MODEL", "hash")
    path = tmp_path / "source.sqlite"
    vault = open_main_vault(str(path), "source-key")
    kept = add_event(vault, "note", "portable memory", source="manual",
                     timestamp="2026-09-17T14:00:00Z", project="ops",
                     metadata={"url": "https://example.org"})
    removed = add_event(vault, "note", "remove me", source="manual")
    forget_events(vault, [removed])
    return vault, path, kept, removed


def test_restore_new_destination_preserves_fields_without_source_key(source, tmp_path):
    vault, path, kept, removed = source
    with sqlite3.connect(path) as conn:
        original_created = conn.execute("SELECT created_at FROM vectors WHERE doc_id = ?", (kept,)).fetchone()[0]
    archive = tmp_path / "portable.lsb"
    result = export_backup(vault, archive, "long backup passphrase")
    assert result["records"] == 1 and result["deleted_ids"] == 1
    assert b"portable memory" not in archive.read_bytes()
    target = tmp_path / "restored.sqlite"
    restored = restore_backup(archive, target, "long backup passphrase", "destination-key")
    assert restored["source_model"] == result["source_model"]
    reopened = open_main_vault(str(target), "destination-key")
    event = get_events(reopened, [kept])[0]
    assert (event["text"], event["timestamp"], event["project"]) == (
        "portable memory", "2026-09-17T14:00:00.000000Z", "ops")
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT created_at FROM vectors WHERE doc_id = ?", (kept,)).fetchone()[0] == original_created
        assert conn.execute(
            "SELECT restored_from_model FROM collection_meta WHERE name = 'clipboard'"
        ).fetchone()[0] == result["source_model"]
    with pytest.raises(ValueError, match="deleted"):
        add_event(reopened, "note", "remove me", source="manual", doc_id=removed)
    with pytest.raises(PermissionError):
        open_main_vault(str(target), "source-key")
    assert fetch_events(vault)["events"][0]["id"] == kept


def test_wrong_passphrase_tamper_and_existing_destination_leave_source(source, tmp_path):
    vault, _, kept, _ = source
    archive = tmp_path / "portable.lsb"
    export_backup(vault, archive, "long backup passphrase")
    destination = tmp_path / "restored.sqlite"
    with pytest.raises(ValueError, match="authentication"):
        restore_backup(archive, destination, "wrong passphrase", "destination-key")
    assert not destination.exists()
    data = bytearray(archive.read_bytes())
    data[-1] ^= 1
    archive.write_bytes(data)
    with pytest.raises(ValueError, match="authentication"):
        restore_backup(archive, destination, "long backup passphrase", "destination-key")
    assert not destination.exists()
    assert get_events(vault, [kept])[0]["text"] == "portable memory"


def test_unreadable_source_aborts_before_output(source, tmp_path):
    vault, path, kept, _ = source
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE vectors SET document = ? WHERE doc_id = ?", ("enc:v2:broken", kept))
    archive = tmp_path / "bad.lsb"
    with pytest.raises(ValueError, match=kept):
        export_backup(vault, archive, "long backup passphrase")
    assert not archive.exists()
