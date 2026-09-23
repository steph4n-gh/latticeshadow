"""The reproducible crash journey runs with only synthetic vault data."""
import json
import hashlib
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


def test_artifact_query_driver_uses_packaged_launcher_contract(tmp_path):
    root = Path(__file__).resolve().parents[3]
    script = root / "packages/cli/scripts/validate_lifecycle.py"
    vault = tmp_path / "synthetic-vault"
    vault.mkdir()
    (vault / "shadow.sqlite").write_bytes(b"driver fixture; no product data")
    key = vault / ".key"
    key.write_text("a" * 64)
    key.chmod(0o600)
    launcher = tmp_path / "LatticeShadow"
    launcher.write_text(f"""#!{sys.executable}
import json, os, sys
if 'PYTHONPATH' in os.environ or sys.argv[1:3] != ['--cli', 'mcp']:
    sys.exit(3)
if sys.argv[3:5] == ['grant', 'create']:
    print(json.dumps({{'id': 'grant_synthetic'}}))
elif sys.argv[3:5] == ['grant', 'preview']:
    print(json.dumps({{'count': 8}}))
elif sys.argv[3] == 'serve':
    for line in sys.stdin:
        request = json.loads(line)
        result = ({{'protocolVersion': '2025-06-18'}} if request['method'] == 'initialize'
                  else {{'structuredContent': {{'events': [{{'id': 'synthetic'}}]}}}})
        print(json.dumps({{'jsonrpc': '2.0', 'id': request['id'], 'result': result}}), flush=True)
else:
    sys.exit(4)
""")
    launcher.chmod(0o755)
    artifact = tmp_path / "candidate.zip"
    artifact.write_bytes(b"synthetic candidate archive")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(root / "packages/cli"), str(root / "packages/db")))
    result = subprocess.run(
        [sys.executable, str(script), "artifact-query", "--app", str(launcher),
         "--artifact", str(artifact), "--vault-dir", str(vault),
         "--expected-events", "8", "--warmup", "1", "--queries", "2"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout)
    assert report["detail"]["events"] == 8
    assert report["detail"]["measured_queries"] == 2
    assert report["detail"]["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert str(tmp_path) not in result.stdout
