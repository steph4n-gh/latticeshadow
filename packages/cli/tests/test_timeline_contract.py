"""Canonical event contract on an isolated hash-model vault."""
import sqlite3
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from latticeshadow.timeline import (
    add_event, assign_project, expire_events, fetch_events, forget_events,
    get_events, search_events, should_capture,
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
    add_event(vault, "note", "safe event", source="manual")
    assert len(fetch_events(vault, scope={"sources": ["manual"]})["events"]) == 1
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


def test_reader_open_before_separate_writer_process(tmp_path, monkeypatch):
    monkeypatch.setenv("LATTICESHADOW_EMBEDDING_MODEL", "hash")
    path = str(tmp_path / "cross-process.sqlite")
    reader = open_main_vault(path, "disposable-key")
    root = Path(__file__).resolve().parents[3]
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(root / "packages/db"), str(root / "packages/cli")]))
    writer = """
import sys
from latticeshadow.vaults import open_main_vault
from latticeshadow.timeline import add_event, forget_events
vault = open_main_vault(sys.argv[1], 'disposable-key')
if sys.argv[2] == 'add':
    print(add_event(vault, 'note', 'separate process memory', source='manual'))
else:
    forget_events(vault, [sys.argv[3]])
"""
    added = subprocess.run([sys.executable, "-c", writer, path, "add"],
                           env=environment, capture_output=True, text=True, check=True)
    doc_id = added.stdout.strip().splitlines()[-1]
    assert search_events(reader, "separate process memory", scope={"sources": ["manual"]})[0]["id"] == doc_id
    subprocess.run([sys.executable, "-c", writer, path, "delete", doc_id],
                   env=environment, capture_output=True, text=True, check=True)
    replacement = subprocess.run([sys.executable, "-c", writer, path, "add"],
                                 env=environment, capture_output=True, text=True, check=True)
    replacement_id = replacement.stdout.strip().splitlines()[-1]
    assert replacement_id != doc_id
    matches = search_events(reader, "separate process memory", scope={"sources": ["manual"]})
    assert [event["id"] for event in matches] == [replacement_id]
    assert get_events(reader, [doc_id]) == []


def test_retention_uses_occurrence_time_and_literal_exclusions(vaults):
    vault, _ = vaults
    old = add_event(vault, "note", "old memory", source="manual",
                    timestamp="2026-01-01T00:00:00Z")
    new = add_event(vault, "note", "new memory", source="manual",
                    timestamp="2026-09-01T00:00:00Z")
    assert should_capture("A secret TOKEN appears", "manual",
                          excluded_literals=["token"]) is False
    assert should_capture("plain", "clipboard", excluded_sources=["clipboard"]) is False
    assert should_capture("plain", "manual", excluded_literals=["token"]) is True
    result = expire_events(vault, before="2026-06-01T00:00:00Z")
    assert result["canonical_deleted"] == 1
    assert get_events(vault, [old]) == []
    assert get_events(vault, [new])[0]["id"] == new


def test_conflicting_retry_race_rechecks_canonical_row(vaults, monkeypatch):
    vault, path = vaults
    rival = open_main_vault(path, "disposable-key")
    original_add = vault.add

    def racing_add(*args, **kwargs):
        add_event(rival, "note", "rival content", source="manual", doc_id="same-id")
        return original_add(*args, **kwargs)

    monkeypatch.setattr(vault, "add", racing_add)
    with pytest.raises(ValueError, match="different content"):
        add_event(vault, "note", "my content", source="manual", doc_id="same-id")
    assert get_events(rival, ["same-id"])[0]["text"] == "rival content"
