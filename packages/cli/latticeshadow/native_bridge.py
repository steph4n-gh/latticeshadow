"""Native macOS intent contract for LatticeShadow.

The Swift companion should stay thin: every App Intent delegates to a stable
`shadow ...` command so the CLI remains the product contract.
"""

from __future__ import annotations

from typing import Any


FOUNDATION_BRIDGE_ENV = "LATTICESHADOW_FOUNDATION_MODELS_CMD"
FOUNDATION_BRIDGE_EXECUTABLE = "native/.build/release/latticeshadow-foundation-bridge"


INTENTS: list[dict[str, Any]] = [
    {
        "name": "RecallMemory",
        "title": "Recall LatticeShadow Memory",
        "command": ["timeline", "--query", "$query", "--json"],
        "parameters": [{"name": "query", "type": "String", "required": True}],
    },
    {
        "name": "PasteMemory",
        "title": "Paste LatticeShadow Recall",
        "command": ["paste", "$query"],
        "parameters": [{"name": "query", "type": "String", "required": True}],
    },
    {
        "name": "SummarizeContext",
        "title": "Summarize Current Context",
        "command": ["now", "--json"],
        "parameters": [],
    },
    {
        "name": "ForgetMemory",
        "title": "Forget LatticeShadow Memory",
        "command": ["forget", "--id", "$id", "--yes"],
        "parameters": [{"name": "id", "type": "String", "required": True}],
    },
    {
        "name": "OpenSourceContext",
        "title": "Open LatticeShadow Source Context",
        "command": ["open-context", "$query"],
        "parameters": [{"name": "query", "type": "String", "required": True}],
    },
]


def intent_contract() -> dict[str, Any]:
    return {
        "schema": "latticeshadow.native_intents.v1",
        "principle": "Every native action delegates to a shadow CLI command.",
        "intents": INTENTS,
        "foundation_bridge": foundation_bridge_contract(),
    }


def swift_intent_source_hint() -> str:
    return "native/LatticeShadowIntents/LatticeShadowIntents.swift"


def foundation_bridge_contract() -> dict[str, Any]:
    return {
        "environment": FOUNDATION_BRIDGE_ENV,
        "package": "native/Package.swift",
        "executable": FOUNDATION_BRIDGE_EXECUTABLE,
        "protocol": {
            "stdin_json": {"task": "summarize", "events": ["event text"]},
            "stdout": "summary text",
        },
        "fallback": "Python extractive summarizer remains active when the bridge is unavailable.",
    }
