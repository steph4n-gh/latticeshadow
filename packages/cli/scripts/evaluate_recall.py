#!/usr/bin/env python3
"""Compare production recall with legacy and deliberately simple baselines.

Only authored synthetic events are inserted. Reports and temporary databases live
outside Git. This is a relevance study, not the 10,000-event latency benchmark.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

CLI_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CLI_DIR.parents[1]
sys.path[:0] = [str(CLI_DIR), str(REPO_ROOT / "packages" / "db")]

from evaluation.scenarios import SCENARIOS
from latticeshadow.retrieval import RetrievalIndex
from latticeshadow.timeline import search_events
from latticeshadow.vaults import EMBEDDING_DIM, embedding_model_id, open_main_vault, _local_model


_TOKENS = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
_STOP = frozenset("a an and are as at be by can did do does for from how i in is it my of on or our should the this to was we were what when where which who why with".split())


def words(text: str) -> set[str]:
    return {word for word in _TOKENS.findall(text.lower()) if word not in _STOP}


def records() -> list[dict[str, str]]:
    rows = []
    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    sources = ("manual", "terminal", "clipboard", "file", "url")
    for pos, case in enumerate(SCENARIOS):
        timestamp = (start + timedelta(days=pos)).isoformat().replace("+00:00", "Z")
        source = sources[pos % len(sources)]
        rows.append({
            "id": f"{case.family}/{case.slug}", "text": case.record,
            "project": case.family, "source": source, "timestamp": timestamp,
            "kind": "scenario",
        })
        # A copied note is a second event with equivalent content. Its ID is
        # labeled relevant as well; it must not manufacture a false failure.
        if case.answerable and pos % 7 == 0:
            rows.append({
                "id": f"{case.family}/{case.slug}/copy", "text": case.record,
                "project": case.family, "source": "clipboard",
                "timestamp": (start + timedelta(days=pos, hours=1)).isoformat().replace("+00:00", "Z"),
                "kind": "duplicate",
            })
    rows.extend(_DECOYS)
    return rows


_DECOYS = [
    {"id": f"decoy/{i:02d}", "text": text, "project": "miscellany", "source": "manual",
     "timestamp": (datetime(2026, 6, 1, 12, tzinfo=timezone.utc) + timedelta(days=i)).isoformat().replace("+00:00", "Z"),
     "kind": "distractor"}
    for i, text in enumerate((
        "The deploy planning meeting moved to Monday; no command was recorded.",
        "A draft rollback checklist asks who owns the release, without naming a revision.",
        "The database design review compared two column naming conventions.",
        "A migration idea was deferred; no backfill has been scheduled.",
        "The support queue summary counted open cases but omitted case resolutions.",
        "The refund policy document explains eligibility without recording a payment.",
        "The 0.9 release brainstorm included several possible changelog sections.",
        "The packaging spike notes that archives need checksums and inspection.",
        "The network team drew a staging topology but recorded no DNS cutover.",
        "The VPN test captured a route proposal without a gateway decision.",
        "Travel packing list: badge, charger, headphones and printed agenda.",
        "A hotel comparison mentions breakfast but contains no reservation.",
        "The quarterly budget draft lists categories without approved limits.",
        "An invoice template has empty transaction and due-date fields.",
        "A calendar note says to arrange a dental visit later this autumn.",
        "A package tracking page was bookmarked without a delivery slot.",
        "A home renovation article discusses paint finishes and color palettes.",
        "The plumber's FAQ lists common repair warranties without a quote.",
        "A garden guide compares tomato varieties but contains no order.",
        "The roof maintenance checklist asks what inspectors might find.",
        "The furniture catalog lists shelf dimensions but no purchase.",
        "A security rotation checklist was opened but no rotation was confirmed.",
        "The outage template has empty root-cause and duration fields.",
        "A patch announcement draft awaits the final version and rollout date.",
        "The capacity spreadsheet is a proposed model without production settings.",
        "A disaster recovery runbook describes a future restore rehearsal.",
        "The CI runner troubleshooting page describes cache and clock issues.",
        "A docs review requested clearer descriptions of project labels.",
        "A code review discusses the timeline API but makes no operational claim.",
        "The onboarding design shows consent switches in their off positions.",
        "A log line says the archive upload was retried after a network timeout.",
        "The database profiler notes a read-only query took 20 milliseconds.",
        "The app panel design compares fonts and empty-state illustrations.",
        "The assistant sharing draft asks whether citations should show source times.",
        "A terminal note records pwd and ls without a project assignment.",
        "A blank scratchpad was saved before the sprint planning session.",
        "The metrics dashboard was viewed but no alert threshold changed.",
        "The customer meeting agenda lists accessibility and exports for discussion.",
        "The upgrade guide says backups are useful before schema changes.",
        "A generated test fixture contains a harmless example URL.",
    ))
]


def keyword_ranking(query: str, docs: list[dict[str, str]]) -> list[str]:
    """IDF-weighted token overlap in memory; never written as a plaintext index."""
    document_words = [words(doc["text"]) for doc in docs]
    frequency = Counter(token for tokens in document_words for token in tokens)
    qwords = words(query)
    n = len(docs)
    scored = []
    for doc, tokens in zip(docs, document_words):
        score = sum(math.log((n + 1) / (frequency[token] + 1)) + 1 for token in qwords & tokens)
        if score > 0:
            scored.append((doc["id"], score))
    return [doc_id for doc_id, _ in sorted(scored, key=lambda pair: (-pair[1], pair[0]))]


def _metrics(cases: list[dict], rankings: dict[str, list[str]]) -> dict:
    answerable = [case for case in cases if case["relevant"]]
    missing = [case for case in cases if not case["relevant"]]
    hits = []
    reciprocal = []
    failures = []
    for case in answerable:
        ranked = rankings[case["id"]]
        rank = next((i + 1 for i, doc_id in enumerate(ranked[:10]) if doc_id in case["relevant"]), None)
        hits.append(rank is not None and rank <= 5)
        reciprocal.append(1 / rank if rank is not None else 0)
        if rank is None or rank > 5:
            failures.append({"query_id": case["id"], "query": case["query"], "expected": sorted(case["relevant"]), "top5": ranked[:5]})
    return {
        "answerable_queries": len(answerable),
        "no_answer_queries": len(missing),
        "hit_at_5": sum(hits) / len(hits) if hits else None,
        "mrr_at_10": sum(reciprocal) / len(reciprocal) if reciprocal else None,
        "no_answer_top5_nonempty": sum(bool(rankings[case["id"]][:5]) for case in missing),
        "failures": failures,
    }


def _cases(split: str) -> list[dict]:
    result = []
    for scenario in SCENARIOS:
        if split != "all" and scenario.split != split:
            continue
        relevant = {f"{scenario.family}/{scenario.slug}"} if scenario.answerable else set()
        if scenario.answerable and SCENARIOS.index(scenario) % 7 == 0:
            relevant.add(f"{scenario.family}/{scenario.slug}/copy")
        for i, query in enumerate(scenario.questions):
            result.append({"id": f"{scenario.family}/{scenario.slug}/{i + 1}", "query": query,
                           "relevant": relevant, "split": scenario.split})
    return result


def evaluate(db_path: str) -> dict:
    if os.environ.get("LATTICESHADOW_EMBEDDING_MODEL") == "hash":
        raise RuntimeError("real-model evaluation cannot run with hash embeddings")
    docs = records()
    cases = _cases("all")
    vault = open_main_vault(db_path, "synthetic-evaluation-key")
    start = time.perf_counter()
    vault.add(
        documents=[doc["text"] for doc in docs],
        ids=[doc["id"] for doc in docs],
        metadatas=[{key: doc[key] for key in ("project", "source", "timestamp", "kind")} for doc in docs],
    )
    ingest_seconds = time.perf_counter() - start
    model = _local_model()
    # Exact comparison uses the *same pinned model* as the encrypted vault.
    # Batch encoding is an evaluation optimization, not a product latency claim.
    doc_vectors = np.asarray(model.encode([doc["text"] for doc in docs], normalize_embeddings=True, truncate_dim=EMBEDDING_DIM), dtype=np.float32)
    query_vectors = np.asarray(model.encode([case["query"] for case in cases], normalize_embeddings=True, truncate_dim=EMBEDDING_DIM), dtype=np.float32)
    vector_lookup = {doc["text"]: vector for doc, vector in zip(docs, doc_vectors)}
    vector_lookup.update({case["query"]: vector for case, vector in zip(cases, query_vectors)})
    ranker = RetrievalIndex(lambda texts: np.asarray([vector_lookup[text] for text in texts], dtype=np.float32))
    ranker.refresh(docs, revision=1)
    all_ids = [doc["id"] for doc in docs]
    results = {"production_scoped": {}, "legacy_encrypted_hybrid": {},
               "exact_cosine": {}, "keyword_overlap": {}, "memory_fusion": {}}
    timings = Counter()
    for case, qvector in zip(cases, query_vectors):
        query_id, query = case["id"], case["query"]
        t0 = time.perf_counter()
        results["production_scoped"][query_id] = [event["id"] for event in
                                                  search_events(vault, query, limit=10)]
        timings["production_scoped"] += time.perf_counter() - t0
        t0 = time.perf_counter()
        results["legacy_encrypted_hybrid"][query_id] = list(vault.search(query, n_results=10, hybrid=True).ids)
        timings["legacy_encrypted_hybrid"] += time.perf_counter() - t0
        t0 = time.perf_counter()
        similarities = doc_vectors @ qvector
        order = np.argsort(-similarities, kind="stable")[:10]
        results["exact_cosine"][query_id] = [docs[int(i)]["id"] for i in order]
        timings["exact_cosine"] += time.perf_counter() - t0
        t0 = time.perf_counter()
        results["keyword_overlap"][query_id] = keyword_ranking(query, docs)[:10]
        timings["keyword_overlap"] += time.perf_counter() - t0
        t0 = time.perf_counter()
        results["memory_fusion"][query_id] = [doc_id for doc_id, _ in ranker.rank(query, all_ids, limit=10)]
        timings["memory_fusion"] += time.perf_counter() - t0
    source = Path(__file__).resolve().parents[1] / "evaluation" / "scenarios.py"
    return {
        "schema": 1, "fixture_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "model": embedding_model_id(), "corpus_events": len(docs),
        "duplicate_events": sum(doc["kind"] == "duplicate" for doc in docs),
        "distractor_events": sum(doc["kind"] == "distractor" for doc in docs),
        "ingest_seconds": ingest_seconds,
        "method_query_seconds_total": dict(timings),
        "methods": {
            method: {split: _metrics([case for case in cases if case["split"] == split], ranked)
                     for split in ("development", "heldout")}
            for method, ranked in results.items()
        },
        "limitations": [
            "Synthetic authored corpus and paraphrases are not a sample of a user's private history.",
            "The legacy privacy-mode hybrid baseline indexes ciphertext; production recall uses an ephemeral plaintext index with canonical rechecks.",
            "Exact cosine uses batch model encoding; timings are not user-visible search latency.",
            "No-answer top-five output is reported separately; nearest-neighbor output is not abstention.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("benchmark_results/recall.json"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="latticeshadow-recall-") as temp:
        report = evaluate(str(Path(temp) / "corpus.sqlite"))
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {args.output} ({report['corpus_events']} synthetic events, {sum(len(s.questions) for s in SCENARIOS)} queries)")
    for method, splits in report["methods"].items():
        h = splits["heldout"]
        print(f"{method}: held-out hit@5={h['hit_at_5']:.3f} MRR@10={h['mrr_at_10']:.3f}; no-answer returned top5={h['no_answer_top5_nonempty']}/{h['no_answer_queries']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
