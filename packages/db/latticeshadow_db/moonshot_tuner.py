"""Promotion-grid tuner for the LatticeShadow moonshot retrieval harness."""

from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .scale_benchmarks import ScaleHarnessConfig, run_scale_harness


@dataclass(frozen=True)
class DiskAnnKnobs:
    partitions: int
    probes: int
    candidates: int
    graph_degree: int = 16
    full_scan_threshold: int = 4096
    kmeans_iterations: int = 1

    def slug(self) -> str:
        return (
            f"p{self.partitions}_pr{self.probes}_c{self.candidates}_"
            f"g{self.graph_degree}_fs{self.full_scan_threshold}_km{self.kmeans_iterations}"
        )


@dataclass(frozen=True)
class TuningStage:
    name: str
    count: int
    dim: int
    queries: int
    promote_top: int
    chunk_size: int = 4096
    insert_chunk_size: int = 2048


@dataclass(frozen=True)
class TuningConfig:
    stages: tuple[TuningStage, ...]
    grid: tuple[DiskAnnKnobs, ...]
    k: int = 10
    seed: int = 42
    recall_gate: float = 0.95
    latency_gate_ms: float = 50.0
    speedup_gate: float = 1000.0


def default_quick_grid() -> tuple[DiskAnnKnobs, ...]:
    return tuple(
        DiskAnnKnobs(partitions=p, probes=pr, candidates=c, graph_degree=g)
        for p, pr, c, g in itertools.product(
            (8, 16),
            (4, 8),
            (64, 128),
            (8, 16),
        )
    )


def default_serious_grid() -> tuple[DiskAnnKnobs, ...]:
    return tuple(
        DiskAnnKnobs(partitions=p, probes=pr, candidates=c, graph_degree=g)
        for p, pr, c, g in itertools.product(
            (32, 64, 128, 256),
            (4, 8, 16, 32),
            (100, 200, 500, 1000, 2000),
            (8, 16, 32),
        )
        if pr <= p
    )


def quick_config() -> TuningConfig:
    return TuningConfig(
        stages=(
            TuningStage("filter", count=1_000, dim=64, queries=5, promote_top=4, chunk_size=512, insert_chunk_size=256),
            TuningStage("promote", count=2_000, dim=96, queries=5, promote_top=2, chunk_size=512, insert_chunk_size=256),
        ),
        grid=default_quick_grid(),
        k=10,
    )


def moonshot_config() -> TuningConfig:
    return TuningConfig(
        stages=(
            TuningStage("filter_10k", count=10_000, dim=128, queries=10, promote_top=12),
            TuningStage("tune_100k", count=100_000, dim=384, queries=25, promote_top=4, chunk_size=8192, insert_chunk_size=4096),
            TuningStage("moonshot_1m", count=1_000_000, dim=768, queries=50, promote_top=1, chunk_size=16384, insert_chunk_size=8192),
        ),
        grid=default_serious_grid(),
        k=10,
    )


def _safe_link_or_copy(source: Path, dest: Path) -> None:
    if dest.is_symlink() and not dest.exists():
        dest.unlink()
    if dest.exists():
        return
    source = source.resolve()
    try:
        os.symlink(source, dest)
    except OSError:
        shutil.copy2(source, dest)


def _prepare_candidate_workspace(base_workspace: Path, candidate_workspace: Path) -> None:
    candidate_workspace.mkdir(parents=True, exist_ok=True)
    source = base_workspace / "synthetic_vectors_f32.bin"
    if source.exists():
        _safe_link_or_copy(source, candidate_workspace / source.name)


def _mode_item(result: dict, mode: str) -> dict:
    for item in result.get("modes", []):
        if item.get("mode") == mode:
            return item
    raise KeyError(f"mode {mode!r} missing from result")


def _score_candidate(item: dict) -> tuple:
    speedup = float(item.get("p95_speedup_vs_dense_public", 0.0))
    latency = float(item.get("p95_ms", float("inf")))
    storage = int(item.get("storage_bytes", 0))
    return (bool(item.get("pass_recall_gate")), speedup, -latency, -storage)


def run_stage(
    stage: TuningStage,
    knobs: tuple[DiskAnnKnobs, ...],
    config: TuningConfig,
    workspace: Path,
) -> dict:
    stage_workspace = workspace / stage.name
    stage_workspace.mkdir(parents=True, exist_ok=True)
    baseline_workspace = stage_workspace / "_baseline"
    baseline_config = ScaleHarnessConfig(
        count=stage.count,
        dim=stage.dim,
        queries=stage.queries,
        k=config.k,
        seed=config.seed,
        modes=("dense_exact_public", "dense_exact_streaming"),
        chunk_size=stage.chunk_size,
        insert_chunk_size=stage.insert_chunk_size,
        recall_gate=config.recall_gate,
        latency_gate_ms=config.latency_gate_ms,
        speedup_gate=config.speedup_gate,
    )
    baseline_result = run_scale_harness(baseline_config, workspace=baseline_workspace)
    baseline = _mode_item(baseline_result, "dense_exact_public")

    candidates = []
    for knob in knobs:
        candidate_workspace = stage_workspace / knob.slug()
        _prepare_candidate_workspace(baseline_workspace, candidate_workspace)
        candidate_config = ScaleHarnessConfig(
            count=stage.count,
            dim=stage.dim,
            queries=stage.queries,
            k=config.k,
            seed=config.seed,
            modes=("diskann_rerank",),
            chunk_size=stage.chunk_size,
            insert_chunk_size=stage.insert_chunk_size,
            diskann_partitions=knob.partitions,
            diskann_probes=knob.probes,
            diskann_candidates=knob.candidates,
            diskann_graph_degree=knob.graph_degree,
            diskann_full_scan_threshold=knob.full_scan_threshold,
            diskann_kmeans_iterations=knob.kmeans_iterations,
            recall_gate=config.recall_gate,
            latency_gate_ms=config.latency_gate_ms,
            speedup_gate=config.speedup_gate,
        )
        result = run_scale_harness(candidate_config, workspace=candidate_workspace)
        item = _mode_item(result, "diskann_rerank")
        item["knobs"] = asdict(knob)
        item["p50_speedup_vs_dense_public"] = (
            baseline["p50_ms"] / item["p50_ms"] if item["p50_ms"] > 0 else 0.0
        )
        item["p95_speedup_vs_dense_public"] = (
            baseline["p95_ms"] / item["p95_ms"] if item["p95_ms"] > 0 else 0.0
        )
        item["pass_recall_gate"] = item.get("recall_at_k", 0.0) >= config.recall_gate
        item["pass_latency_gate"] = item.get("p95_ms", float("inf")) <= config.latency_gate_ms
        candidates.append(item)

    ranked = sorted(candidates, key=_score_candidate, reverse=True)
    promoted = [
        DiskAnnKnobs(**item["knobs"])
        for item in ranked
        if item.get("pass_recall_gate")
    ][:stage.promote_top]
    stage_result = {
        "stage": asdict(stage),
        "baseline": baseline,
        "ranked": ranked,
        "promoted": [asdict(item) for item in promoted],
    }
    (stage_workspace / "leaderboard.json").write_text(
        json.dumps(stage_result, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return stage_result


def run_tuning_loop(config: TuningConfig, workspace: Path) -> dict:
    workspace.mkdir(parents=True, exist_ok=True)
    started = time.time()
    active_grid = config.grid
    stages = []
    for stage in config.stages:
        result = run_stage(stage, active_grid, config, workspace)
        stages.append(result)
        active_grid = tuple(DiskAnnKnobs(**item) for item in result["promoted"])
        if not active_grid:
            break
    summary = {
        "schema_version": 1,
        "started_at": started,
        "elapsed_s": time.time() - started,
        "config": {
            "k": config.k,
            "seed": config.seed,
            "recall_gate": config.recall_gate,
            "latency_gate_ms": config.latency_gate_ms,
            "speedup_gate": config.speedup_gate,
            "stages": [asdict(stage) for stage in config.stages],
            "grid_size": len(config.grid),
        },
        "stages": stages,
        "best": stages[-1]["ranked"][0] if stages and stages[-1]["ranked"] else None,
    }
    (workspace / "leaderboard.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (workspace / "leaderboard.md").write_text(render_markdown(summary), encoding="utf-8")
    return summary


def render_markdown(summary: dict) -> str:
    lines = ["# Moonshot Tuning Leaderboard", ""]
    for stage in summary.get("stages", []):
        stage_name = stage["stage"]["name"]
        lines.append(f"## {stage_name}")
        lines.append("")
        lines.append("| Rank | Recall | p95 ms | p95 speedup | p50 speedup | Knobs |")
        lines.append("|---:|---:|---:|---:|---:|---|")
        for idx, item in enumerate(stage.get("ranked", [])[:10], start=1):
            knobs = item.get("knobs", {})
            knob_text = (
                f"p={knobs.get('partitions')} pr={knobs.get('probes')} "
                f"c={knobs.get('candidates')} g={knobs.get('graph_degree')} "
                f"fs={knobs.get('full_scan_threshold')} km={knobs.get('kmeans_iterations')}"
            )
            lines.append(
                f"| {idx} | {item.get('recall_at_k', 0.0):.3f} | "
                f"{item.get('p95_ms', 0.0):.3f} | "
                f"{item.get('p95_speedup_vs_dense_public', 0.0):.2f} | "
                f"{item.get('p50_speedup_vs_dense_public', 0.0):.2f} | {knob_text} |"
            )
        lines.append("")
    return "\n".join(lines)


def _parse_csv_ints(text: str) -> tuple[int, ...]:
    return tuple(int(part.strip()) for part in text.split(",") if part.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", choices=("quick", "moonshot"), default="quick")
    parser.add_argument("--workspace", type=Path, default=Path("benchmark_results/moonshot_tuning"))
    parser.add_argument("--partitions", default=None, help="Comma-separated partition counts.")
    parser.add_argument("--probes", default=None, help="Comma-separated probe counts.")
    parser.add_argument("--candidates", default=None, help="Comma-separated candidate caps.")
    parser.add_argument("--graph-degrees", default=None, help="Comma-separated graph degrees.")
    parser.add_argument("--full-scan-thresholds", default=None, help="Comma-separated full-scan cutoffs.")
    parser.add_argument("--kmeans-iterations", default=None, help="Comma-separated k-means iteration counts.")
    parser.add_argument("--torch-threads", type=int, default=None)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.torch_threads is not None:
        import torch

        torch.set_num_threads(args.torch_threads)
    config = moonshot_config() if args.preset == "moonshot" else quick_config()
    if (
        args.partitions
        or args.probes
        or args.candidates
        or args.graph_degrees
        or args.full_scan_thresholds
        or args.kmeans_iterations
    ):
        partitions = _parse_csv_ints(args.partitions) if args.partitions else (8, 16)
        probes = _parse_csv_ints(args.probes) if args.probes else (4, 8)
        candidates = _parse_csv_ints(args.candidates) if args.candidates else (64, 128)
        graph_degrees = _parse_csv_ints(args.graph_degrees) if args.graph_degrees else (8, 16)
        full_scan_thresholds = (
            _parse_csv_ints(args.full_scan_thresholds)
            if args.full_scan_thresholds
            else (4096,)
        )
        kmeans_iterations = (
            _parse_csv_ints(args.kmeans_iterations)
            if args.kmeans_iterations
            else (1,)
        )
        grid = tuple(
            DiskAnnKnobs(
                partitions=p,
                probes=pr,
                candidates=c,
                graph_degree=g,
                full_scan_threshold=fs,
                kmeans_iterations=km,
            )
            for p, pr, c, g, fs, km in itertools.product(
                partitions,
                probes,
                candidates,
                graph_degrees,
                full_scan_thresholds,
                kmeans_iterations,
            )
            if pr <= p
        )
        config = TuningConfig(
            stages=config.stages,
            grid=grid,
            k=config.k,
            seed=config.seed,
            recall_gate=config.recall_gate,
            latency_gate_ms=config.latency_gate_ms,
            speedup_gate=config.speedup_gate,
        )
    summary = run_tuning_loop(config, args.workspace)
    print(json.dumps({
        "workspace": str(args.workspace),
        "best": summary.get("best"),
        "elapsed_s": summary.get("elapsed_s"),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
