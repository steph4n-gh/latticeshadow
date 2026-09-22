import importlib.util
import json
from pathlib import Path


def _load_runner():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_retrieval_leaderboard.py"
    spec = importlib.util.spec_from_file_location("run_retrieval_leaderboard", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_retrieval_leaderboard_writes_json_and_markdown(tmp_path):
    runner = _load_runner()

    code = runner.main(
        [
            "--workspace",
            str(tmp_path),
            "--small-count",
            "16",
            "--small-dim",
            "8",
            "--small-queries",
            "2",
            "--small-mode",
            "dense_exact",
            "--small-mode",
            "cascade_auto",
            "--scale-count",
            "24",
            "--scale-dim",
            "8",
            "--scale-queries",
            "2",
            "--scale-mode",
            "dense_exact_public",
            "--scale-mode",
            "cascade_auto",
            "--k",
            "2",
            "--chunk-size",
            "8",
            "--insert-chunk-size",
            "8",
            "--torch-threads",
            "1",
        ]
    )

    assert code == 0
    leaderboard = json.loads((tmp_path / "leaderboard.json").read_text(encoding="utf-8"))
    assert (tmp_path / "leaderboard.md").exists()
    assert leaderboard["small"]["vector_store"][1]["mode"] == "cascade_auto"
    assert {item["mode"] for item in leaderboard["scale"]["modes"]} == {
        "dense_exact_public",
        "cascade_auto",
    }
