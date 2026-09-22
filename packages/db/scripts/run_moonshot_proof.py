"""Run or summarize the LatticeShadow retrieval moonshot proof."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import torch

from latticeshadow_db.scale_benchmarks import (
    AVAILABLE_MODES,
    ScaleHarnessConfig,
    result_path,
    run_scale_harness,
)


DEFAULT_MODES = ("dense_exact_public", "streaming_exact_public")


def _git_output(args: list[str]) -> str:
    try:
        return subprocess.check_output(args, text=True).strip()
    except Exception:
        return ""


def _mode(result: dict, name: str) -> dict:
    for item in result.get("modes", []):
        if item.get("mode") == name:
            return item
    return {}


def build_summary(result: dict, report: Path, label: str, command: list[str]) -> dict:
    best_mode = result.get("gates", {}).get("best_mode")
    best = _mode(result, best_mode) if best_mode else {}
    dense = _mode(result, "dense_exact_public")
    environment = dict(result.get("environment") or {})
    environment.setdefault("python", platform.python_version())
    environment.setdefault("platform", platform.platform())
    environment.setdefault("torch", torch.__version__)
    environment.setdefault("torch_num_threads", torch.get_num_threads())
    return {
        "label": label,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "git_sha": _git_output(["git", "rev-parse", "HEAD"]),
        "git_branch": _git_output(["git", "branch", "--show-current"]),
        "command": command,
        "report": str(report),
        "environment": environment,
        "gates": result.get("gates", {}),
        "best": {
            "mode": best.get("mode"),
            "recall_at_k": best.get("recall_at_k"),
            "p50_ms": best.get("p50_ms"),
            "p95_ms": best.get("p95_ms"),
            "p99_ms": best.get("p99_ms"),
            "max_ms": best.get("max_ms"),
            "p50_speedup_vs_dense_public": best.get("p50_speedup_vs_dense_public"),
            "p95_speedup_vs_dense_public": best.get("p95_speedup_vs_dense_public"),
            "build_ms": best.get("build_ms"),
            "cold_reload_ms": best.get("cold_reload_ms"),
            "storage_bytes": best.get("storage_bytes"),
        },
        "dense_exact_public": {
            "p50_ms": dense.get("p50_ms"),
            "p95_ms": dense.get("p95_ms"),
            "p99_ms": dense.get("p99_ms"),
            "max_ms": dense.get("max_ms"),
            "storage_bytes": dense.get("storage_bytes"),
        },
        "modes": [
            {
                "mode": item.get("mode"),
                "recall_at_k": item.get("recall_at_k"),
                "mrr_at_k": item.get("mrr_at_k"),
                "ndcg_at_k": item.get("ndcg_at_k"),
                "p50_ms": item.get("p50_ms"),
                "p95_ms": item.get("p95_ms"),
                "p99_ms": item.get("p99_ms"),
                "max_ms": item.get("max_ms"),
                "p50_speedup_vs_dense_public": item.get("p50_speedup_vs_dense_public"),
                "p95_speedup_vs_dense_public": item.get("p95_speedup_vs_dense_public"),
                "storage_bytes": item.get("storage_bytes"),
                "build_ms": item.get("build_ms"),
                "cold_reload_ms": item.get("cold_reload_ms"),
                "experimental_index": item.get("experimental_index"),
                "accelerator_missing": item.get("accelerator_missing"),
                "fallback_reason": item.get("fallback_reason"),
                "latencies_ms": item.get("latencies_ms"),
            }
            for item in result.get("modes", [])
        ],
    }


def _fmt(value: object, digits: int = 3) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.{digits}f}"
    return "n/a"


def render_markdown(summary: dict) -> str:
    gates = summary.get("gates", {})
    best = summary.get("best", {})
    lines = [
        f"# Moonshot Proof: {summary['label']}",
        "",
        f"- Git SHA: `{summary.get('git_sha', '')}`",
        f"- Branch: `{summary.get('git_branch', '')}`",
        f"- Report: `{summary.get('report', '')}`",
        f"- Achieved: `{gates.get('achieved')}`",
        f"- Target: `{gates.get('target_count')} x {gates.get('target_dim')}`, queries `{gates.get('target_queries')}`",
        f"- Warmup queries per mode: `{gates.get('warmup_queries', 0)}`",
        f"- Best mode: `{best.get('mode')}`",
        f"- Best p50/p95: `{_fmt(best.get('p50_ms'))} ms` / `{_fmt(best.get('p95_ms'))} ms`",
        f"- Best p50/p95 speedup: `{_fmt(best.get('p50_speedup_vs_dense_public'), 2)}x` / `{_fmt(best.get('p95_speedup_vs_dense_public'), 2)}x`",
        f"- Best p99/max: `{_fmt(best.get('p99_ms'))} ms` / `{_fmt(best.get('max_ms'))} ms`",
        f"- Best build/cold reload: `{_fmt(best.get('build_ms'))} ms` / `{_fmt(best.get('cold_reload_ms'))} ms`",
        f"- Recall@k: `{best.get('recall_at_k')}`",
        f"- Storage bytes: `{best.get('storage_bytes')}`",
        "",
        "| Mode | Recall | p50 ms | p95 ms | p99 ms | Max ms | p50 speedup | p95 speedup | Cold reload ms | Storage bytes | Fallback |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in summary.get("modes", []):
        lines.append(
            f"| {item.get('mode')} | {_fmt(item.get('recall_at_k'))} | "
            f"{_fmt(item.get('p50_ms'))} | {_fmt(item.get('p95_ms'))} | "
            f"{_fmt(item.get('p99_ms'))} | {_fmt(item.get('max_ms'))} | "
            f"{_fmt(item.get('p50_speedup_vs_dense_public'), 2)} | "
            f"{_fmt(item.get('p95_speedup_vs_dense_public'), 2)} | "
            f"{_fmt(item.get('cold_reload_ms'))} | "
            f"{item.get('storage_bytes')} | "
            f"{item.get('fallback_reason') if item.get('accelerator_missing') else ''} |"
        )
    lines.extend(["", "## Command", "", "```bash", " ".join(summary.get("command", [])), "```", ""])
    return "\n".join(lines)


def write_summary(summary: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "proof_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "proof_summary.md").write_text(render_markdown(summary), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=None, help="Summarize an existing scale_report.json.")
    parser.add_argument("--workspace", type=Path, default=Path("benchmark_results/moonshot_proof"))
    parser.add_argument("--summary-dir", type=Path, default=None)
    parser.add_argument("--label", default="moonshot-proof")
    parser.add_argument("--count", type=int, default=1_000_000)
    parser.add_argument("--dim", type=int, default=768)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--warmup-queries", type=int, default=0)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--chunk-size", type=int, default=4096)
    parser.add_argument("--insert-chunk-size", type=int, default=2048)
    parser.add_argument("--mode", dest="modes", action="append", choices=AVAILABLE_MODES)
    parser.add_argument("--hnsw-m", type=int, default=32)
    parser.add_argument("--hnsw-ef-construction", type=int, default=200)
    parser.add_argument("--hnsw-ef-search", type=int, default=128)
    parser.add_argument("--hnsw-candidates", type=int, default=1000)
    parser.add_argument("--torch-threads", type=int, default=None)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    command = [sys.executable, __file__, *(list(argv) if argv is not None else sys.argv[1:])]
    if args.torch_threads is not None:
        torch.set_num_threads(args.torch_threads)

    if args.report:
        report = args.report
        result = json.loads(report.read_text(encoding="utf-8"))
    else:
        config = ScaleHarnessConfig(
            count=args.count,
            dim=args.dim,
            queries=args.queries,
            warmup_queries=args.warmup_queries,
            k=args.k,
            modes=tuple(args.modes or DEFAULT_MODES),
            chunk_size=args.chunk_size,
            insert_chunk_size=args.insert_chunk_size,
            hnsw_m=args.hnsw_m,
            hnsw_ef_construction=args.hnsw_ef_construction,
            hnsw_ef_search=args.hnsw_ef_search,
            hnsw_candidates=args.hnsw_candidates,
        )
        result = run_scale_harness(config, workspace=args.workspace)
        report = result_path(args.workspace)

    summary = build_summary(result, report, args.label, command)
    output_dir = args.summary_dir or (args.workspace / "proof_summary")
    write_summary(summary, output_dir)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
