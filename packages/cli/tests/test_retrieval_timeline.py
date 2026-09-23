"""The ranking index receives only eligible IDs and rechecks canonical results."""
from latticeshadow.retrieval import RetrievalIndex
from latticeshadow.timeline import (
    add_event, assign_project, clear_search_cache, forget_events, search_events,
)
from latticeshadow.vaults import open_main_vault


def test_search_scope_and_revisioned_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("LATTICESHADOW_EMBEDDING_MODEL", "hash")
    path = str(tmp_path / "retrieval.sqlite")
    vault = open_main_vault(path, "disposable-key")
    left = add_event(vault, "note", "deployment fix left", source="manual", project="left")
    right = add_event(vault, "note", "deployment fix right", source="manual", project="right")
    calls = []
    original_rank = RetrievalIndex.rank

    def observe_rank(self, query, candidate_ids, limit=10):
        candidate_ids = list(candidate_ids)
        calls.append(candidate_ids)
        return original_rank(self, query, candidate_ids, limit)

    monkeypatch.setattr(RetrievalIndex, "rank", observe_rank)
    try:
        assert [event["id"] for event in search_events(
            vault, "deployment fix", scope={"projects": ["left"]})] == [left]
        assert calls[-1] == [left]
        assert assign_project(vault, [left], "right") == 1
        assert search_events(vault, "deployment fix", scope={"projects": ["left"]}) == []
        assert {event["id"] for event in search_events(
            vault, "deployment fix", scope={"projects": ["right"]})} == {left, right}
        forget_events(vault, [left])
        assert [event["id"] for event in search_events(
            vault, "deployment fix", scope={"projects": ["right"]})] == [right]
    finally:
        clear_search_cache()
