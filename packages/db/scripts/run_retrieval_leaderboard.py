#!/usr/bin/env python3
"""Run unified retrieval, privacy, and scale benchmark leaderboards."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Iterable

import torch

from latticeshadow_db.benchmarks import HarnessConfig, run_harness
from latticeshadow_db.scale_benchmarks import ScaleHarnessConfig, run_scale_harness


DEFAULT_SMALL_MODES = (
    "dense_exact",
    "privacy_dense",
    "hnsw_exact_rerank",
    "flyhash_exact_rerank",
    "pq_exact_rerank",
    "cascade_auto",
)
DEFAULT_SCALE_MODES = (
    "dense_exact_public",
    "dense_exact_streaming",
    "streaming_exact_public",
    "hnsw_exact_rerank",
    "pq_exact_rerank",
    "cascade_auto",
)


def _fmt(value: object, digits: int = 3) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.{digits}f}"
    return "n/a"


def _mode_table(rows: list[dict], latency_key: str, p95_key: str) -> list[str]:
    lines = [
        "| Mode | Recall | p50 ms | p95 ms | Storage bytes | Index | Fallback | Plaintext sidecar bytes |",
        "|---|---:|---:|---:|---:|---|---|---:|",
    ]
    for item in rows:
        audit = item.get("sidecar_audit") or {}
        fallback = item.get("fallback_reason") if item.get("accelerator_missing") else ""
        lines.append(
            f"| {item.get('mode')} | {_fmt(item.get('recall_at_k'))} | "
            f"{_fmt(item.get(latency_key))} | {_fmt(item.get(p95_key))} | "
            f"{item.get('storage_bytes')} | {item.get('experimental_index')} | "
            f"{fallback} | "
            f"{audit.get('plaintext_sidecar_bytes', 0)} |"
        )
    return lines


def render_markdown(leaderboard: dict) -> str:
    lines = [
        f"# Retrieval Leaderboard: {leaderboard['label']}",
        "",
        f"- Created: `{leaderboard['created_at']}`",
        f"- Workspace: `{leaderboard['workspace']}`",
        f"- Command: `{' '.join(leaderboard['command'])}`",
        "",
        "## Optional Accelerators",
        "",
        "```json",
        json.dumps(leaderboard.get("optional_accelerators", {}), indent=2, sort_keys=True),
        "```",
        "",
    ]
    small = leaderboard.get("small")
    if small:
        lines.extend(["## Small Privacy Harness", ""])
        lines.extend(_mode_table(small.get("vector_store", []), "search_p50_ms", "search_p95_ms"))
        leakage = small.get("privacy_leakage", {})
        lines.extend(
            [
                "",
                "### Privacy Leakage",
                "",
                "| Metric | Value |",
                "|---|---:|",
            ]
        )
        for key in sorted(leakage):
            value = leakage[key]
            if isinstance(value, (int, float, str, bool)) or value is None:
                lines.append(f"| {key} | {value} |")
    scale = leaderboard.get("scale")
    if scale:
        gates = scale.get("gates", {})
        lines.extend(
            [
                "",
                "## Scale Harness",
                "",
                f"- Best mode: `{gates.get('best_mode')}`",
                f"- Achieved gates: `{gates.get('achieved')}`",
                f"- Target: `{gates.get('target_count')} x {gates.get('target_dim')}`",
                "",
            ]
        )
        lines.extend(_mode_table(scale.get("modes", []), "p50_ms", "p95_ms"))
        lines.extend(
            [
                "",
                "| Mode | p50 speedup vs dense | p95 speedup vs dense | Cold reload ms |",
                "|---|---:|---:|---:|",
            ]
        )
        for item in scale.get("modes", []):
            lines.append(
                f"| {item.get('mode')} | "
                f"{_fmt(item.get('p50_speedup_vs_dense_public'), 2)} | "
                f"{_fmt(item.get('p95_speedup_vs_dense_public'), 2)} | "
                f"{_fmt(item.get('cold_reload_ms'))} |"
            )
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path("benchmark_results/retrieval_leaderboard"))
    parser.add_argument("--label", default="mega-retrieval-cascade")
    parser.add_argument("--skip-small", action="store_true")
    parser.add_argument("--skip-scale", action="store_true")
    parser.add_argument("--small-count", type=int, default=1000)
    parser.add_argument("--small-dim", type=int, default=128)
    parser.add_argument("--small-queries", type=int, default=10)
    parser.add_argument("--small-mode", dest="small_modes", action="append")
    parser.add_argument("--scale-count", type=int, default=10_000)
    parser.add_argument("--scale-dim", type=int, default=128)
    parser.add_argument("--scale-queries", type=int, default=10)
    parser.add_argument("--scale-warmup-queries", type=int, default=0)
    parser.add_argument("--scale-mode", dest="scale_modes", action="append")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--chunk-size", type=int, default=4096)
    parser.add_argument("--insert-chunk-size", type=int, default=2048)
    parser.add_argument("--hnsw-m", type=int, default=32)
    parser.add_argument("--hnsw-ef-construction", type=int, default=200)
    parser.add_argument("--hnsw-ef-search", type=int, default=128)
    parser.add_argument("--hnsw-candidates", type=int, default=1000)
    parser.add_argument("--torch-threads", type=int, default=None)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.torch_threads is not None:
        torch.set_num_threads(args.torch_threads)

    workspace = args.workspace
    workspace.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, __file__, *(list(argv) if argv is not None else sys.argv[1:])]
    leaderboard = {
        "label": args.label,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "workspace": str(workspace),
        "command": command,
    }

    if not args.skip_small:
        small_config = HarnessConfig(
            count=args.small_count,
            dim=args.small_dim,
            queries=args.small_queries,
            k=args.k,
            seed=args.seed,
            modes=tuple(args.small_modes or DEFAULT_SMALL_MODES),
            privacy_known_pairs=min(args.small_count - 1, max(args.small_dim * 2, args.k + 1)),
        )
        small = run_harness(small_config, workspace=workspace / "small")
        leaderboard["small"] = small
        leaderboard["optional_accelerators"] = small.get("environment", {}).get("optional_accelerators", {})

    if not args.skip_scale:
        scale_config = ScaleHarnessConfig(
            count=args.scale_count,
            dim=args.scale_dim,
            queries=args.scale_queries,
            warmup_queries=args.scale_warmup_queries,
            k=args.k,
            seed=args.seed,
            modes=tuple(args.scale_modes or DEFAULT_SCALE_MODES),
            chunk_size=args.chunk_size,
            insert_chunk_size=args.insert_chunk_size,
            hnsw_m=args.hnsw_m,
            hnsw_ef_construction=args.hnsw_ef_construction,
            hnsw_ef_search=args.hnsw_ef_search,
            hnsw_candidates=args.hnsw_candidates,
        )
        scale = run_scale_harness(scale_config, workspace=workspace / "scale")
        leaderboard["scale"] = scale
        leaderboard.setdefault(
            "optional_accelerators",
            scale.get("environment", {}).get("optional_accelerators", {}),
        )

    json_path = workspace / "leaderboard.json"
    md_path = workspace / "leaderboard.md"
    json_path.write_text(json.dumps(leaderboard, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_markdown(leaderboard), encoding="utf-8")
    print(json.dumps({"leaderboard_json": str(json_path), "leaderboard_md": str(md_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
