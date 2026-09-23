#!/usr/bin/env python3
"""Exercise a synthetic granted vault through the independent MCP Python SDK.

Run in an environment with the optional `mcp` SDK installed. The product does
not require the SDK merely to serve stdio.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from mcp import Client, StdioServerParameters

from latticeshadow.sharing import create_grant, revoke_grant
from latticeshadow.timeline import add_event
from latticeshadow.vaults import open_main_vault

_DIRECT = """
import sys
from pathlib import Path
from latticeshadow.mcp_server import serve
from latticeshadow.vaults import open_main_vault
root, grant_id = sys.argv[1:]
serve(lambda: open_main_vault(str(Path(root) / 'shadow.sqlite'),
                             (Path(root) / '.key').read_text().strip()),
      str(Path(root) / 'shadow.sqlite'), root, grant_id=grant_id)
"""


async def validate(server_python: str, *, direct: bool = False) -> dict:
    with tempfile.TemporaryDirectory(prefix="latticeshadow-mcp-sdk-") as location:
        root = Path(location)
        key = secrets.token_hex(32)
        key_file = root / ".key"
        key_file.write_text(key)
        key_file.chmod(0o600)
        vault = open_main_vault(str(root / "shadow.sqlite"), key)
        allowed = add_event(vault, "note", "Synthetic deployment note for recall",
                            source="manual", project="ops",
                            timestamp="2026-09-17T14:00:00Z")
        add_event(vault, "note", "Synthetic private finance note",
                  source="manual", project="finance",
                  timestamp="2026-09-17T15:00:00Z")
        grant = create_grant(root, projects=["ops"], sources=["manual"], limit=5)
        if direct:
            command = server_python
            arguments = ["-c", _DIRECT, str(root), grant["id"]]
        else:
            command = str(Path(server_python).with_name("shadow"))
            if not Path(command).is_file():
                raise FileNotFoundError(f"installed shadow command missing next to {server_python}")
            arguments = ["mcp", "serve",
                         "--grant", grant["id"], "--vault-dir", str(root)]
        environment = {"LATTICESHADOW_EMBEDDING_MODEL": "hash"}
        if os.environ.get("PYTHONPATH"):
            environment["PYTHONPATH"] = os.environ["PYTHONPATH"]
        server = StdioServerParameters(command=command, args=arguments, env=environment)
        async with Client(server, mode="legacy", read_timeout_seconds=20) as client:
            assert client.protocol_version == "2025-06-18", client.protocol_version
            tools = await client.list_tools()
            names = {item.name for item in tools.tools}
            assert {"latticeshadow.recall", "latticeshadow.resolve"} <= names
            assert "latticeshadow.forget" not in names
            recall = await client.call_tool("latticeshadow.recall", {"query": "deployment", "limit": 5})
            assert recall.is_error is not True
            events = recall.structured_content["events"]
            assert [item["id"] for item in events] == [allowed], events
            uri = events[0]["citation"]
            resource = await client.read_resource(uri)
            assert json.loads(resource.contents[0].text)["event"]["id"] == allowed
            prompt = await client.get_prompt("recall-with-citations")
            assert "Cite" in prompt.messages[0].content.text
            assert revoke_grant(root, grant["id"])
            denied = None
            try:
                denied = await client.call_tool("latticeshadow.recall", {"query": "deployment"})
            except Exception as exc:
                assert "Sharing grant unavailable" in str(exc), str(exc)
            else:
                assert denied.is_error is True
            try:
                await client.read_resource(uri)
            except Exception as exc:
                assert "Sharing grant unavailable" in str(exc), str(exc)
            else:
                raise AssertionError("revoked citation remained available")
        return {"protocol": "2025-06-18", "sdk": version("mcp"),
                "server": "direct serve() subprocess" if direct else "installed CLI entry point",
                "allowed_event": "resolved and cited", "outside_scope": "excluded",
                "revocation": "enforced in same session"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-python", default=sys.executable,
                        help="Python executable with latticeshadow-cli and DB installed")
    parser.add_argument("--direct", action="store_true",
                        help="Exercise serve() directly before CLI integration")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(validate(args.server_python, direct=args.direct)), indent=2))


if __name__ == "__main__":
    main()
