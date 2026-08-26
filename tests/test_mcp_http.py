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


def test_manifest_mounts_the_workspace_skills_tree():
    """load_skill reads SKILLS_DIR off the container filesystem. Without this
    volume nothing ever put the workspace skills there, so load_skill failed
    for every skill and agents built around it ran with no instructions."""
    import json, pathlib
    m = json.loads((pathlib.Path(__file__).parent.parent / "aw-app.json").read_text())
    vols = {v["source"]: v for v in m["runtime"]["volumes"]}
    assert vols["$AW_WORKSPACE_SKILLS"]["target"] == "/app/skills"
    assert vols["$AW_WORKSPACE_SKILLS"]["mode"] == "ro"
    assert m["runtime"]["env"]["KB_SKILLS_DIR"] == "/app/skills"


# ---- Per-profile scope (gateway-injected _gateway_kb_index) ----------------


def _call(tool, arguments):
    return mcp_http.handle_request({
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    })["result"]


def _doc(repo, path, content="body"):
    return {"score": 0.9, "content": content,
            "metadata": {"repo": repo, "path": path, "source": f"{repo}/{path}"}}


def _fake_index(monkeypatch, docs):
    from kb_app import kb_pg
    monkeypatch.setattr(kb_pg, "count", lambda: len(docs))
    monkeypatch.setattr(kb_pg, "search", lambda q, n_results=5: docs[:n_results])


def test_scoped_search_returns_only_the_allowed_repo(monkeypatch):
    _fake_index(monkeypatch, [_doc("aw-workspace", "a.md"), _doc("crispal", "b.md")])

    text = _call("search_knowledge_base",
                 {"query": "x", "_gateway_kb_index": "crispal"})["content"][0]["text"]

    assert "crispal/b.md" in text and "aw-workspace/a.md" not in text
    assert "in 'crispal/'" in text  # the answer says which slice it searched


def test_scoped_search_accepts_several_repos(monkeypatch):
    _fake_index(monkeypatch, [_doc("aw-workspace", "a.md"), _doc("crispal", "b.md"),
                              _doc("docs", "c.md")])

    text = _call("search_knowledge_base",
                 {"query": "x", "_gateway_kb_index": ["crispal", "docs"]})["content"][0]["text"]

    assert "crispal/b.md" in text and "docs/c.md" in text
    assert "aw-workspace/a.md" not in text


def test_scoped_search_with_no_match_in_scope_is_an_explicit_miss(monkeypatch):
    _fake_index(monkeypatch, [_doc("aw-workspace", "a.md")])

    result = _call("search_knowledge_base", {"query": "x", "_gateway_kb_index": "crispal"})

    assert result["isError"] is True
    assert "in 'crispal/'" in result["content"][0]["text"]


def test_unscoped_search_still_sees_every_repo(monkeypatch):
    _fake_index(monkeypatch, [_doc("aw-workspace", "a.md"), _doc("crispal", "b.md")])

    text = _call("search_knowledge_base", {"query": "x"})["content"][0]["text"]

    assert "aw-workspace/a.md" in text and "crispal/b.md" in text


def test_a_scoped_write_cannot_escape_its_folder(monkeypatch):
    written = {}
    monkeypatch.setattr(mcp_http.kb_ops, "update",
                        lambda path, content: written.setdefault("path", path))

    _call("update_knowledge_base",
          {"path": "../aw-workspace/secrets.md", "content": "x",
           "_gateway_kb_index": "crispal"})

    # Not "crispal/../aw-workspace/secrets.md" — that reads as scoped and
    # normalises straight back out of the scope.
    assert written["path"] == "crispal/aw-workspace/secrets.md"


def test_a_scoped_write_already_in_scope_is_left_alone(monkeypatch):
    written = {}
    monkeypatch.setattr(mcp_http.kb_ops, "update",
                        lambda path, content: written.setdefault("path", path))

    _call("update_knowledge_base",
          {"path": "crispal/note.md", "content": "x", "_gateway_kb_index": "crispal"})

    assert written["path"] == "crispal/note.md"


def test_a_scoped_delete_is_forced_under_the_scope_too(monkeypatch):
    deleted = {}
    monkeypatch.setattr(mcp_http.kb_ops, "delete",
                        lambda path: deleted.setdefault("path", path) or True)

    _call("delete_knowledge_base", {"path": "note.md", "_gateway_kb_index": "crispal"})

    assert deleted["path"] == "crispal/note.md"


def test_an_unscoped_write_keeps_the_path_it_was_given(monkeypatch):
    written = {}
    monkeypatch.setattr(mcp_http.kb_ops, "update",
                        lambda path, content: written.setdefault("path", path))

    _call("update_knowledge_base", {"path": "memory/note.md", "content": "x"})

    assert written["path"] == "memory/note.md"
