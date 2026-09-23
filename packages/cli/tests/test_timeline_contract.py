"""Canonical event contract on an isolated hash-model vault."""
import sqlite3
from datetime import datetime, timezone

import pytest

from latticeshadow.timeline import (
    add_event, assign_project, fetch_events, forget_events, get_events, search_events,
)
from latticeshadow.vaults import open_main_vault


@pytest.fixture
def vaults(tmp_path, monkeypatch):
    monkeypatch.setenv("LATTICESHADOW_EMBEDDING_MODEL", "hash")
    path = str(tmp_path / "events.sqlite")
    return open_main_vault(path, "disposable-key"), path


def test_occurrence_pagination_scope_and_retry(vaults):
    vault, _ = vaults
    first = add_event(vault, "terminal", "deploy first", source="terminal",
                      timestamp=datetime(2026, 9, 17, 14, tzinfo=timezone.utc),
                      project="ops", doc_id="command-1", metadata={"shell": "zsh"})
    second = add_event(vault, "note", "deploy later", source="manual",
                       timestamp="2026-09-17T15:00:00+01:00", project=None)
    assert first == "command-1" and second != first
    assert add_event(vault, "terminal", "deploy first", source="terminal",
                     project="ops", doc_id=first, metadata={"shell": "zsh"}) == first
    with pytest.raises(ValueError, match="different content"):
        add_event(vault, "terminal", "changed", source="terminal", project="ops", doc_id=first)
    page = fetch_events(vault, limit=1)
    assert page["events"][0]["id"] == second
    following = fetch_events(vault, limit=1, cursor=page["next_cursor"])
    assert following["events"][0]["id"] == first
    assert following["next_cursor"] is None
    assert fetch_events(vault, scope={"projects": [None]})["events"][0]["id"] == second
    assert [e["id"] for e in get_events(vault, [first, second], scope={"projects": ["ops"]})] == [first]
    assert search_events(vault, "deploy", scope={"projects": ["ops"]})[0]["id"] == first
    with pytest.raises(ValueError, match="cursor"):
        fetch_events(vault, scope={"projects": ["ops"]}, cursor=page["next_cursor"])
    assert assign_project(vault, [second], "ops") == 1
    assert get_events(vault, [second])[0]["project"] == "ops"


def test_cross_reader_delete_and_tombstone(vaults):
    writer, path = vaults
    reader = open_main_vault(path, "disposable-key")
    doc_id = add_event(writer, "note", "a secret command", source="manual")
    assert search_events(reader, "secret command", scope={"sources": ["manual"]})[0]["id"] == doc_id
    assert forget_events(writer, [doc_id]) == {
        "canonical_deleted": 1, "derived_invalidated": True, "cleanup_errors": []}
    assert get_events(reader, [doc_id]) == []
    assert search_events(reader, "secret command", scope={"sources": ["manual"]}) == []
    with pytest.raises(ValueError, match="deleted"):
        add_event(reader, "note", "a secret command", source="manual", doc_id=doc_id)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT 1 FROM deleted_ids WHERE doc_id = ?", (doc_id,)).fetchone()


def test_legacy_time_and_strict_decryption(vaults):
    vault, path = vaults
    vault.add(documents=["legacy command"], ids=["cmd_legacy"], metadatas=[{}])
    event = get_events(vault, ["cmd_legacy"])[0]
    assert event["source"] == "unknown"
    assert event["type"] == "terminal"
    assert event["metadata"]["timestamp_inferred"] is True
    assert event["timestamp"].endswith("Z")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE vectors SET document = ? WHERE doc_id = ?", ("enc:v2:not-base64", "cmd_legacy"))
    with pytest.raises(ValueError, match="decrypt"):
        get_events(vault, ["cmd_legacy"])


def test_input_bounds_and_empty_scope(vaults):
    vault, _ = vaults
    with pytest.raises(ValueError, match="timezone"):
        add_event(vault, "note", "text", timestamp="2026-09-17T15:00:00")
    with pytest.raises(ValueError, match="event text"):
        add_event(vault, "note", "x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="metadata"):
        add_event(vault, "note", "text", metadata={"oversized": "x" * (64 * 1024)})
    assert fetch_events(vault, scope={"projects": []}) == {"events": [], "next_cursor": None}
