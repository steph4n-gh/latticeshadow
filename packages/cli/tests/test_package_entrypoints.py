"""Check installed commands and the actual stdio protocol boundary."""

import json
import io
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path


def test_installed_console_entrypoints_resolve():
    commands = {
        ep.name: ep for ep in distribution("latticeshadow-cli").entry_points
        if ep.group == "console_scripts"
    }
    for name in ("shadow", "latticeshadow", "shadowd"):
        assert callable(commands[name].load())
    result = subprocess.run(
        [str(Path(sys.executable).with_name("shadow")), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "timeline" in result.stdout


def test_mcp_stdio_initialization_and_tool_discovery():
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "latticeshadow-test", "version": "1"},
        }},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    result = subprocess.run(
        [sys.executable, "-m", "latticeshadow.shadow_cli", "mcp", "serve"],
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert [response["id"] for response in responses] == [1, 2]
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    names = {tool["name"] for tool in responses[1]["result"]["tools"]}
    assert {"latticeshadow.recall", "latticeshadow.current_context", "latticeshadow.forget"} <= names


def test_mcp_transport_recovers_after_bad_input_and_keeps_stdout_clean(capsys):
    from latticeshadow.mcp_server import serve

    def unavailable_vault():
        print("vault diagnostic")
        raise RuntimeError("Test vault unavailable")

    requests = "not-json\n[]\n" + "\n".join(json.dumps(request) for request in [
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "latticeshadow.current_context", "arguments": {},
        }},
    ]) + "\n"
    output = io.StringIO()
    serve(unavailable_vault, "unused.sqlite", "unused", stdin=io.StringIO(requests), stdout=output)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(responses) == 4
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["error"]["code"] == -32600
    assert responses[2] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert responses[3]["error"]["message"] == "Test vault unavailable"
    assert "vault diagnostic" in capsys.readouterr().err
