"""Checks for the frozen relevance task and interpretation of its results."""

from evaluation.scenarios import SCENARIOS
from latticeshadow.retrieval import RetrievalIndex
from scripts.evaluate_recall import _cases, _metrics, keyword_ranking, records
import numpy as np


def test_family_split_and_labels_cover_real_questions():
    development = _cases("development")
    heldout = _cases("heldout")
    assert len(development) == 125
    assert sum(bool(case["relevant"]) for case in development) == 100
    assert sum(not case["relevant"] for case in development) == 25
    assert len(heldout) == 250
    assert sum(bool(case["relevant"]) for case in heldout) == 200
    assert sum(not case["relevant"] for case in heldout) == 50
    assert {s.family for s in SCENARIOS if s.split == "development"}.isdisjoint(
        {s.family for s in SCENARIOS if s.split == "heldout"}
    )
    record_ids = {item["id"] for item in records()}
    for case in development + heldout:
        assert case["query"].endswith("?") or len(case["query"].split()) >= 5
        assert case["relevant"] <= record_ids


def test_duplicate_labels_and_negative_queries_are_not_accidental():
    docs = records()
    duplicates = [item for item in docs if item["kind"] == "duplicate"]
    assert len(duplicates) >= 5
    answer_labels = {item for case in _cases("all") for item in case["relevant"]}
    assert {item["id"] for item in duplicates} <= answer_labels
    negative = [case for case in _cases("heldout") if not case["relevant"]]
    assert {case["id"].split("/")[0] for case in negative} == {
        "unrecorded-home", "unrecorded-ops"
    }


def test_metrics_do_not_count_unreturned_answers_or_no_answer_as_success():
    cases = [
        {"id": "found", "query": "find a", "relevant": {"a"}},
        {"id": "absent", "query": "find b", "relevant": {"b"}},
        {"id": "unknown", "query": "find c", "relevant": set()},
    ]
    report = _metrics(cases, {"found": ["x", "a"], "absent": ["x"], "unknown": ["x"]})
    assert report["hit_at_5"] == 0.5
    assert report["mrr_at_10"] == 0.25
    assert report["no_answer_top5_nonempty"] == 1
    assert len(report["failures"]) == 1


def test_keyword_baseline_uses_lexical_evidence_only():
    docs = [{"id": "a", "text": "restart alder service"},
            {"id": "b", "text": "repair birch table"}]
    assert keyword_ranking("restart alder", docs)[0] == "a"
    assert keyword_ranking("unrelated telescope", docs) == []


def test_in_memory_ranker_respects_prefiltered_candidates_and_revision_changes():
    encoded = {"alder rollback": [1, 0], "birch release": [0, 1],
               "rollback alder": [1, 0], "birch incident": [0, 1]}
    calls = []

    def encode(texts):
        calls.extend(texts)
        return np.asarray([encoded[text] for text in texts], dtype=np.float32)

    index = RetrievalIndex(encode)
    events = [{"id": "a", "text": "alder rollback"}, {"id": "b", "text": "birch release"}]
    index.refresh(events, revision=1)
    assert [item[0] for item in index.rank("rollback alder", ["b", "a"])] == ["a", "b"]
    assert index.rank("rollback alder", ["b"])[0][0] == "b"
    index.refresh(events, revision=1)
    assert calls.count("alder rollback") == 1
    index.refresh([events[0], {"id": "b", "text": "birch incident"}], revision=2)
    assert calls.count("alder rollback") == 1
    assert calls.count("birch incident") == 1
    index.refresh([events[0]], revision=3)
    assert index.rank("rollback alder", ["a"])[0][0] == "a"
    assert "b" not in index._events
    index.clear()
    assert index.revision is None
    assert index._events == {}
