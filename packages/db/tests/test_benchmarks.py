import json

import pytest
import torch

from latticeshadow_db.benchmarks import (
    HarnessConfig,
    binary_auc,
    gram_relative_error,
    known_plaintext_recovery_error,
    mean_jaccard,
    pairwise_distance_relative_error,
    recall_at_k,
    run_distillation_replay_metrics,
    run_harness,
)
from latticeshadow_db.adapter import CayleyPrivacyAdapter


def test_recall_at_k():
    assert recall_at_k(["a", "b", "c"], ["b", "d", "a"], 3) == pytest.approx(2 / 3)
    assert recall_at_k(["a", "b", "c"], ["b", "d", "a"], 2) == pytest.approx(0.5)


def test_mean_jaccard():
    assert mean_jaccard([[1, 2], [3]], [[2, 4], [3]]) == pytest.approx((1 / 3 + 1.0) / 2)


def test_binary_auc_orders_positive_scores_higher():
    assert binary_auc([0.1, 0.2, 0.9, 1.0], [0, 0, 1, 1]) == pytest.approx(1.0)
    assert binary_auc([0.1, 0.2, 0.9, 1.0], [1, 1, 0, 0]) == pytest.approx(0.0)


def test_cayley_rotation_preserves_gram_matrix():
    torch.manual_seed(42)
    raw = torch.randn(12, 8)
    adapter = CayleyPrivacyAdapter(dim=8, rank=4)
    rotated = adapter.rotate(raw)
    assert gram_relative_error(raw, rotated) < 1e-5
    assert pairwise_distance_relative_error(raw, rotated) < 1e-5


def test_known_plaintext_recovery_with_enough_pairs():
    torch.manual_seed(42)
    raw = torch.randn(24, 8)
    adapter = CayleyPrivacyAdapter(dim=8, rank=4)
    rotated = adapter.rotate(raw)
    assert known_plaintext_recovery_error(raw, rotated, known_pairs=16) < 1e-4


def test_run_harness_small_dense_modes(tmp_path):
    config = HarnessConfig(
        count=16,
        dim=8,
        queries=3,
        k=3,
        seed=7,
        modes=("dense_exact", "privacy_dense"),
        privacy_known_pairs=12,
    )
    result = run_harness(config, workspace=tmp_path)
    assert result["schema_version"] == 1
    assert {item["mode"] for item in result["vector_store"]} == {"dense_exact", "privacy_dense"}
    assert all(item["recall_at_k"] >= 0.99 for item in result["vector_store"])
    assert result["privacy_leakage"]["gram_relative_error"] < 1e-5
    assert result["privacy_leakage"]["pairwise_distance_relative_error"] < 1e-5
    assert result["privacy_leakage"]["knn_jaccard_at_k"] >= 0.99
    assert "compression_reconstruction_relative_error" in result["privacy_leakage"]
    assert "srht_pairwise_distortion_p95" in result["privacy_leakage"]


def test_run_harness_explicit_hnsw_rerank_mode(tmp_path):
    config = HarnessConfig(
        count=16,
        dim=8,
        queries=3,
        k=3,
        seed=7,
        modes=("hnsw_rerank",),
        privacy_known_pairs=12,
    )
    result = run_harness(config, workspace=tmp_path)
    item = result["vector_store"][0]
    assert item["mode"] == "hnsw_rerank"
    assert item["experimental_index"] == "hnsw_rerank"
    assert item["hnsw_rerank_enabled"] is True
    assert item["hnsw_rerank_candidate_count"] == 16
    assert item["memmap_capacity"] >= 16
    assert item["cold_memmap_capacity"] >= 16
    assert item["recall_at_k"] >= 0.99


def test_run_harness_explicit_flyhash_rerank_mode(tmp_path):
    config = HarnessConfig(
        count=16,
        dim=8,
        queries=3,
        k=3,
        seed=7,
        modes=("flyhash_rerank",),
        privacy_known_pairs=12,
    )
    result = run_harness(config, workspace=tmp_path)
    item = result["vector_store"][0]
    assert item["mode"] == "flyhash_rerank"
    assert item["experimental_index"] == "flyhash_rerank"
    assert item["flyhash_rerank_enabled"] is True
    assert item["flyhash_rerank_candidate_count"] == 16
    assert item["recall_at_k"] >= 0.99


def test_run_harness_explicit_pq_rerank_mode(tmp_path):
    config = HarnessConfig(
        count=16,
        dim=8,
        queries=3,
        k=3,
        seed=7,
        modes=("pq_rerank",),
        privacy_known_pairs=12,
    )
    result = run_harness(config, workspace=tmp_path)
    item = result["vector_store"][0]
    assert item["mode"] == "pq_rerank"
    assert item["experimental_index"] == "pq_rerank"
    assert item["pq_rerank_enabled"] is True
    assert item["pq_rerank_candidate_count"] == 16
    assert item["pq_code_storage_bytes"] == 100 * 8
    assert item["recall_at_k"] >= 0.99


def test_run_harness_explicit_diskann_rerank_mode(tmp_path):
    config = HarnessConfig(
        count=16,
        dim=8,
        queries=3,
        k=3,
        seed=7,
        modes=("diskann_rerank",),
        privacy_known_pairs=12,
    )
    result = run_harness(config, workspace=tmp_path)
    item = result["vector_store"][0]
    assert item["mode"] == "diskann_rerank"
    assert item["experimental_index"] == "diskann_rerank"
    assert item["diskann_rerank_enabled"] is True
    assert item["diskann_rerank_candidate_count"] == 16
    assert item["diskann_vector_storage_bytes"] == 100 * 8 * 2
    assert item["diskann_graph_storage_bytes"] == 100 * 16 * 4
    assert item["recall_at_k"] >= 0.99


def test_run_harness_reports_new_cascade_alias_modes(tmp_path):
    config = HarnessConfig(
        count=16,
        dim=8,
        queries=3,
        k=3,
        seed=7,
        modes=("hnsw_exact_rerank", "pq_exact_rerank", "cascade_auto"),
        privacy_known_pairs=12,
    )
    result = run_harness(config, workspace=tmp_path)
    modes = {item["mode"]: item for item in result["vector_store"]}

    assert modes["hnsw_exact_rerank"]["experimental_index"] == "hnsw_exact_rerank"
    assert modes["hnsw_exact_rerank"]["hnsw_rerank_enabled"] is True
    assert modes["pq_exact_rerank"]["experimental_index"] == "pq_exact_rerank"
    assert modes["pq_exact_rerank"]["pq_rerank_enabled"] is True
    assert modes["cascade_auto"]["experimental_index"] == "cascade_auto"
    assert modes["cascade_auto"]["cascade_auto_enabled"] is True
    assert modes["cascade_auto"]["recall_at_k"] >= 0.99
    assert "sidecar_audit" in modes["cascade_auto"]


def test_run_harness_reports_flyhash_cascade_aliases(tmp_path):
    config = HarnessConfig(
        count=16,
        dim=8,
        queries=3,
        k=3,
        seed=7,
        modes=("flyhash_exact_rerank", "flyhash_pq_rerank"),
        privacy_known_pairs=12,
    )
    result = run_harness(config, workspace=tmp_path)
    modes = {item["mode"]: item for item in result["vector_store"]}

    assert modes["flyhash_exact_rerank"]["experimental_index"] == "flyhash_exact_rerank"
    assert modes["flyhash_exact_rerank"]["flyhash_rerank_enabled"] is True
    assert modes["flyhash_pq_rerank"]["experimental_index"] == "flyhash_pq_rerank"
    assert modes["flyhash_pq_rerank"]["flyhash_rerank_enabled"] is True
    assert modes["flyhash_pq_rerank"]["sidecar_audit"]["plaintext_sidecar_bytes"] > 0
    assert all(item["recall_at_k"] >= 0.99 for item in modes.values())


def test_run_harness_lattice_reports_compact_code_metrics(tmp_path):
    config = HarnessConfig(
        count=8,
        dim=32,
        queries=2,
        k=2,
        seed=7,
        modes=("lattice",),
        privacy_known_pairs=4,
    )
    result = run_harness(config, workspace=tmp_path)
    item = result["vector_store"][0]

    assert item["mode"] == "lattice"
    assert item["lattice_index_count"] == 8
    assert item["lattice_code_dtype"] == "torch.int16"
    assert item["lattice_code_storage_bytes"] == 8 * 32 * 24 * 2


def test_distillation_replay_metrics_gate_cloud_skips(tmp_path):
    config = HarnessConfig(
        count=24,
        dim=4,
        queries=2,
        k=2,
        seed=11,
        modes=("dense_exact",),
    )
    metrics = run_distillation_replay_metrics(config, workspace=tmp_path)

    assert metrics["stable"]["is_graduated"] is True
    assert metrics["stable"]["cloud_skip_allowed_rate"] == 1.0
    assert metrics["stable"]["pass_fail"] == "pass"
    assert metrics["drifted_tail"]["is_graduated"] is False
    assert metrics["drifted_tail"]["cloud_skip_allowed_rate"] == 0.0
    assert metrics["drifted_tail"]["graduation_block_reason"] == "holdout_loss_above_threshold"
    assert metrics["drifted_tail"]["pass_fail"] == "pass"


def test_script_output_json(tmp_path):
    from latticeshadow_db.benchmarks import main

    output = tmp_path / "bench.json"
    rc = main(
        [
            "--count",
            "12",
            "--dim",
            "8",
            "--queries",
            "2",
            "--k",
            "2",
            "--mode",
            "dense",
            "--output",
            str(output),
        ]
    )
    assert rc == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["config"]["count"] == 12
    assert payload["vector_store"][0]["mode"] == "dense_exact"


def test_cli_all_mode_and_aliases(tmp_path):
    from latticeshadow_db.benchmarks import main

    output = tmp_path / "bench.json"
    workspace = tmp_path / "dbs"
    rc = main(
        [
            "--n",
            "12",
            "--dim",
            "8",
            "--queries",
            "2",
            "--k",
            "2",
            "--mode",
            "all",
            "--db-dir",
            str(workspace),
            "--torch-threads",
            "1",
            "--output",
            str(output),
        ]
    )
    assert rc == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["config"]["modes"] == [
        "dense_exact",
        "privacy_dense",
        "cli_privacy_drosophila_hybrid",
        "lattice",
    ]
