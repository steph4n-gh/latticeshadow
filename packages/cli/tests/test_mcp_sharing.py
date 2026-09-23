"""Synthetic grant, scope, citation and transport boundary checks."""

import io
import json
from pathlib import Path

import pytest

from latticeshadow.mcp_server import CITATION_PREFIX, handle_request, serve
from latticeshadow.sharing import (create_grant, load_grant, preview_grant,
                                   revoke_grant)
from latticeshadow.timeline import add_event, forget_events
from latticeshadow.vaults import open_main_vault


@pytest.fixture
def scenario(tmp_path):
    vault = open_main_vault(str(tmp_path / "memory.sqlite"), "test-only-key")
    allowed = add_event(vault, "note", "deploy with api_key=example-secret-value",
                        source="manual", project="ops",
                        timestamp="2026-09-17T10:00:00Z",
                        metadata={"provenance": {"nested": ["password=do-not-share"]},
                                  "unrelated_secret": "private sidebar"})
    other_project = add_event(vault, "note", "deploy finance production",
                              source="manual", project="finance",
                              timestamp="2026-09-17T11:00:00Z")
    other_source = add_event(vault, "terminal", "deploy terminal command",
                             source="terminal", project="ops",
                             timestamp="2026-09-17T12:00:00Z")
    grant = create_grant(tmp_path, projects=["ops"], sources=["manual"],
                         since="2026-09-17T00:00:00Z", limit=3)
    def ask(method, params=None, *, ceiling=grant):
        return handle_request({"jsonrpc": "2.0", "id": 1, "method": method,
                               "params": params or {}}, lambda: vault,
                              str(tmp_path / "memory.sqlite"), str(tmp_path),
                              grant_id=grant["id"], startup_ceiling=ceiling)
    return vault, grant, allowed, other_project, other_source, ask, tmp_path


def _payload(response):
    return response["result"]["structuredContent"]


def test_grant_is_explicit_and_scoped_before_limit(scenario):
    vault, grant, allowed, other_project, other_source, ask, root = scenario
    assert preview_grant(vault, grant)["count"] == 1
    assert Path(root / "sharing_grants.json").stat().st_mode & 0o777 == 0o600
    for method, params in (
        ("tools/call", {"name": "latticeshadow.current_context", "arguments": {"limit": 50}}),
        ("tools/call", {"name": "latticeshadow.recall", "arguments": {"query": "deploy", "limit": 50}}),
        ("tools/call", {"name": "latticeshadow.summarize", "arguments": {"limit": 50}}),
    ):
        events = _payload(ask(method, params))["events"]
        assert [event["id"] for event in events] == [allowed]
        assert events[0]["citation"] == CITATION_PREFIX + allowed
        assert events[0]["redacted"] is True
        assert "example-secret-value" not in json.dumps(events)
        assert "do-not-share" not in json.dumps(events)
        assert "private sidebar" not in json.dumps(events)
    narrowed = ask("tools/call", {"name": "latticeshadow.recall",
                                   "arguments": {"query": "deploy", "scope": {"projects": ["finance"]}}})
    assert _payload(narrowed)["events"] == []
    assert _payload(ask("tools/call", {"name": "latticeshadow.current_context",
                                     "arguments": {"scope": {"sources": ["terminal"]}}}))["events"] == []
    assert _payload(ask("tools/call", {"name": "latticeshadow.current_context",
                                     "arguments": {"scope": {"since": "2026-09-18T00:00:00Z"}}}))["events"] == []


def test_citations_resolve_live_and_hide_missing_vs_out_of_scope(scenario):
    vault, grant, allowed, other_project, _, ask, _ = scenario
    uri = CITATION_PREFIX + allowed
    assert _payload(ask("tools/call", {"name": "latticeshadow.resolve", "arguments": {"uri": uri}}))["event"]["id"] == allowed
    read = ask("resources/read", {"uri": uri})
    assert json.loads(read["result"]["contents"][0]["text"])["event"]["id"] == allowed
    missing = ask("resources/read", {"uri": CITATION_PREFIX + "never-existed"})
    excluded = ask("resources/read", {"uri": CITATION_PREFIX + other_project})
    assert missing["error"] == excluded["error"] == {"code": -32602, "message": "Event unavailable"}
    assert forget_events(vault, [allowed])["canonical_deleted"] == 1
    assert ask("resources/read", {"uri": uri})["error"] == missing["error"]
    assert ask("tools/call", {"name": "latticeshadow.resolve", "arguments": {"uri": uri}})["result"]["isError"] is True


def test_citation_percent_encodes_caller_supplied_id(scenario):
    vault, _, _, _, _, ask, _ = scenario
    doc_id = add_event(vault, "note", "unusual but valid local reference",
                       source="manual", project="ops", doc_id="manual/ops note")
    uri = CITATION_PREFIX + "manual%2Fops%20note"
    assert _payload(ask("tools/call", {"name": "latticeshadow.resolve",
                                       "arguments": {"uri": uri}}))["event"]["id"] == doc_id
    assert ask("resources/read", {"uri": CITATION_PREFIX + "manual/ops note"})["error"]["message"] == "Event unavailable"


def test_revocation_and_startup_ceiling(scenario):
    _, grant, allowed, _, _, ask, root = scenario
    store = root / "sharing_grants.json"
    content = json.loads(store.read_text())
    content["grants"][0]["projects"].append("finance")
    content["grants"][0]["sources"].append("terminal")
    content["grants"][0]["limit"] = 50
    store.write_text(json.dumps(content))
    assert [event["id"] for event in _payload(ask("tools/call", {
        "name": "latticeshadow.current_context", "arguments": {"limit": 50}}))["events"]] == [allowed]
    assert revoke_grant(root, grant["id"]) is True
    assert load_grant(root, grant["id"]) is None
    assert ask("tools/call", {"name": "latticeshadow.current_context", "arguments": {}})["error"]["message"] == "Sharing grant unavailable"
    assert ask("resources/read", {"uri": CITATION_PREFIX + allowed})["error"]["message"] == "Sharing grant unavailable"
    assert ask("prompts/get", {"name": "recall-with-citations"})["error"]["message"] == "Sharing grant unavailable"


def test_no_grant_is_read_only_and_fails_closed(scenario):
    _, _, allowed, _, _, _, root = scenario
    no_grant = lambda method, params=None: handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        lambda: pytest.fail("vault opened without grant"), "unused", str(root))
    names = {item["name"] for item in no_grant("tools/list")["result"]["tools"]}
    assert names == {"latticeshadow.recall", "latticeshadow.current_context",
                     "latticeshadow.summarize", "latticeshadow.resolve"}
    assert no_grant("tools/call", {"name": "latticeshadow.recall", "arguments": {"query": "deploy"}})["error"]["message"] == "Sharing grant unavailable"
    assert no_grant("resources/read", {"uri": CITATION_PREFIX + allowed})["error"]["message"] == "Sharing grant unavailable"
    assert no_grant("prompts/get", {"name": "recall-with-citations"})["error"]["message"] == "Sharing grant unavailable"
    assert no_grant("tools/call", {"name": "latticeshadow.forget", "arguments": {"ids": [allowed], "confirm": "FORGET"}})["result"]["isError"] is True


def test_arguments_and_frames_are_bounded(scenario):
    _, grant, _, _, _, ask, root = scenario
    for args in ({"query": "deploy", "limit": True},
                 {"query": "deploy", "scope": []},
                 {"query": "deploy", "scope": {"sources": ["manual"], "projects": "ops"}},
                 {"query": "deploy", "extra": "ignored?"}):
        response = ask("tools/call", {"name": "latticeshadow.recall", "arguments": args})
        assert response["result"]["isError"] is True
    too_big = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping",
                          "params": {"padding": "x" * 70_000}}) + "\n"
    valid = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
    output = io.StringIO()
    serve(lambda: pytest.fail("vault opened"), "unused", str(root),
          stdin=io.StringIO(too_big + valid), stdout=output,
          grant_id=grant["id"])
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]["error"]["code"] == -32600
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
