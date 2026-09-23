"""The reproducible crash journey runs with only synthetic vault data."""
import json
import os
import subprocess
import sys
from pathlib import Path


def test_subprocess_lifecycle_report(tmp_path):
    root = Path(__file__).resolve().parents[3]
    script = root / "packages/cli/scripts/validate_lifecycle.py"
    report = tmp_path / "report.json"
    env = dict(os.environ, LATTICESHADOW_EMBEDDING_MODEL="hash")
    env["PYTHONPATH"] = os.pathsep.join((str(root / "packages/cli"), str(root / "packages/db")))
    completed = subprocess.run(
        [sys.executable, str(script), "lifecycle", "--report", str(report)],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["status"] == "passed"
    assert result["detail"]["count"] >= 12
    assert "concurrent-writers-visible-to-existing-reader" in result["detail"]["checks"]
    assert "interrupted-restore-leaves-source-readable" in result["detail"]["checks"]
    assert "cli-grant-and-mcp-citation-forget-restart" in result["detail"]["checks"]
    assert str(tmp_path) not in report.read_text(encoding="utf-8")
    assert "synthetic portable backup passphrase" not in report.read_text(encoding="utf-8")
