import json
from pathlib import Path

from latticeshadow_db.moonshot_tuner import (
    DiskAnnKnobs,
    TuningConfig,
    TuningStage,
    run_tuning_loop,
)


def test_moonshot_tuner_promotes_ranked_diskann_knobs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = Path("relative_tuning")
    config = TuningConfig(
        stages=(
            TuningStage(
                "tiny",
                count=64,
                dim=16,
                queries=3,
                promote_top=1,
                chunk_size=16,
                insert_chunk_size=16,
            ),
        ),
        grid=(
            DiskAnnKnobs(partitions=4, probes=4, candidates=32, graph_degree=8),
            DiskAnnKnobs(partitions=8, probes=4, candidates=64, graph_degree=8),
        ),
        k=5,
        recall_gate=0.8,
    )

    summary = run_tuning_loop(config, workspace)

    stage = summary["stages"][0]
    assert summary["best"]["mode"] == "diskann_rerank"
    assert len(stage["ranked"]) == 2
    assert len(stage["promoted"]) == 1
    assert (workspace / "leaderboard.json").exists()
    assert (workspace / "leaderboard.md").exists()

    persisted = json.loads((workspace / "leaderboard.json").read_text(encoding="utf-8"))
    assert persisted["best"]["knobs"] == summary["best"]["knobs"]
