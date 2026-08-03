"""Tests for kb_app/mcp_http.py's JSON-RPC dispatch (the HTTP-MCP surface
that replaces the original stdio server — see mcp_http.py's module
docstring for why)."""
from __future__ import annotations

from kb_app import mcp_http


def test_initialize():
    resp = mcp_http.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp["result"]["serverInfo"]["name"] == "aw-app-kb"


def test_notifications_return_none():
    assert mcp_http.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_has_all_five_tools():
    resp = mcp_http.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {
        "search_knowledge_base", "update_knowledge_base", "delete_knowledge_base",
        "search_skills", "load_skill",
    }


def test_search_empty_kb_reports_error(monkeypatch):
    from kb_app import kb_pg
    monkeypatch.setattr(kb_pg, "count", lambda: 0)
    resp = mcp_http.handle_request({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "search_knowledge_base", "arguments": {"query": "anything"}},
    })
    result = resp["result"]
    assert result["isError"] is True
    assert "empty" in result["content"][0]["text"].lower()


def test_load_skill_rejects_invalid_name():
    resp = mcp_http.handle_request({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "load_skill", "arguments": {"name": "../../etc/passwd"}},
    })
    assert resp["result"]["isError"] is True


def test_load_skill_reads_from_skills_dir(tmp_path, monkeypatch):
    skill_dir = tmp_path / "aw-kb-curator"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Curator\ncontent")
    monkeypatch.setattr(mcp_http, "SKILLS_DIR", str(tmp_path))

    resp = mcp_http.handle_request({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "load_skill", "arguments": {"name": "aw-kb-curator"}},
    })
    result = resp["result"]
    assert result["isError"] is False
    assert "content" in result["content"][0]["text"]


def test_unknown_tool_reports_error():
    resp = mcp_http.handle_request({
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "nope", "arguments": {}},
    })
    assert resp["result"]["isError"] is True


def test_unknown_method_returns_json_rpc_error():
    resp = mcp_http.handle_request({"jsonrpc": "2.0", "id": 7, "method": "bogus"})
    assert resp["error"]["code"] == -32601
