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


def test_tools_list_has_all_six_tools():
    resp = mcp_http.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {
        "search_knowledge_base", "update_knowledge_base", "delete_knowledge_base",
        "search_skills", "search_execution_history", "load_skill",
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


def test_the_exact_paths_that_leaked_on_2026_09_08(monkeypatch):
    """Card 3d55bf3b-9510-81ea-9a4e-fcd0753602f4's acceptance, literally: the
    two unprefixed paths crispal-codex actually wrote to the workspace root.

    Neither is an escape attempt — that's the point. `_force_scope` handled
    `../` correctly all along (the test above); what reached this code on
    2026-09-08 was an ordinary relative path with NO kb_index beside it,
    because the caller was on the gateway's unscoped root `/mcp` rather than
    `/mcp/crispal-full`. These cases pin the enforcement half of the fix; the
    routing half is pinned in aw-app-agents-platform-runners'
    tests/test_codex_agent_scoped_mcp_config.py.
    """
    written = []
    monkeypatch.setattr(mcp_http.kb_ops, "update",
                        lambda path, content: written.append(path))

    for leaked in ("docs/atendimento/politica-interna-trocas-e-devolucoes.md",
                   "memory/reenvio-apos-devolucao-por-morada-incorreta.md"):
        _call("update_knowledge_base",
              {"path": leaked, "content": "x", "_gateway_kb_index": "crispal"})

    assert written == [
        "crispal/docs/atendimento/politica-interna-trocas-e-devolucoes.md",
        "crispal/memory/reenvio-apos-devolucao-por-morada-incorreta.md",
    ]


def test_a_scope_prefix_is_a_path_boundary_not_a_string_prefix(monkeypatch):
    """`crispal-evil/` starts with `crispal` as a STRING but is a different
    folder — it must be forced under the scope, not waved through as already
    in it."""
    written = {}
    monkeypatch.setattr(mcp_http.kb_ops, "update",
                        lambda path, content: written.setdefault("path", path))

    _call("update_knowledge_base",
          {"path": "crispal-evil/x.md", "content": "x", "_gateway_kb_index": "crispal"})

    assert written["path"] == "crispal/crispal-evil/x.md"


def test_a_scoped_search_returns_nothing_from_the_leaked_repos(monkeypatch):
    """The read half of the same incident: the leaking run's results carried
    Repo: repos / docs / memory. Under kb_index="crispal" a search whose
    candidates are all from those repos must come back as an explicit miss,
    not as somebody else's documents."""
    _fake_index(monkeypatch, [
        _doc("docs", "atendimento/politica-interna-trocas-e-devolucoes.md"),
        _doc("memory", "reenvio-apos-devolucao-por-morada-incorreta.md"),
        _doc("repos", "agentic-workspace/src/config/aw.json"),
    ])

    result = _call("search_knowledge_base",
                   {"query": "politica de trocas", "_gateway_kb_index": "crispal"})

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "in 'crispal/'" in text
    assert "politica-interna" not in text and "reenvio-apos-devolucao" not in text


def test_an_unscoped_write_keeps_the_path_it_was_given(monkeypatch):
    written = {}
    monkeypatch.setattr(mcp_http.kb_ops, "update",
                        lambda path, content: written.setdefault("path", path))

    _call("update_knowledge_base", {"path": "memory/note.md", "content": "x"})

    assert written["path"] == "memory/note.md"


# ---- search_execution_history ----------------------------------------------


def _exec_chunk(run_id, seq=0, content="matched excerpt", status="success", score=0.9):
    return {
        "chunk_id": f"{run_id}:{seq}", "run_id": run_id, "seq": seq, "content": content,
        "metadata": {"status": status, "agent_slug": "coder-sonnet", "target_slug": "t1",
                     "ended_at": "2026-09-04T00:00:00", "cost_usd": 0.42},
        "score": score,
    }


def test_search_execution_history_empty_index_reports_error(monkeypatch):
    from kb_app import exec_pg
    monkeypatch.setattr(exec_pg, "count", lambda: 0)
    result = _call("search_execution_history", {"query": "anything"})
    assert result["isError"] is True
    assert "no execution history" in result["content"][0]["text"].lower()


def test_search_execution_history_groups_chunks_by_run(monkeypatch):
    from kb_app import exec_pg
    monkeypatch.setattr(exec_pg, "count", lambda: 10)
    # Two chunks from the SAME run, one from another — must collapse to 2 blocks.
    monkeypatch.setattr(exec_pg, "search", lambda q, n_results=5, filters=None: [
        _exec_chunk("run-1", seq=0, content="first chunk"),
        _exec_chunk("run-1", seq=1, content="second chunk"),
        _exec_chunk("run-2", seq=0, content="other run"),
    ])

    text = _call("search_execution_history", {"query": "tool error"})["content"][0]["text"]

    assert text.count("--- Run run-1") == 1
    assert text.count("--- Run run-2") == 1
    assert "get_run_detail(run_id='run-1')" in text
    assert "get_run_detail(run_id='run-2')" in text


def test_search_execution_history_passes_filters_through(monkeypatch):
    from kb_app import exec_pg
    monkeypatch.setattr(exec_pg, "count", lambda: 10)
    captured = {}

    def _fake_search(q, n_results=5, filters=None):
        captured["filters"] = filters
        return [_exec_chunk("run-1")]

    monkeypatch.setattr(exec_pg, "search", _fake_search)

    _call("search_execution_history", {
        "query": "x", "status": "error", "agent_slug": "coder-sonnet",
        "target_slug": "t1", "since_days": 7, "run_id": "run-1",
    })

    assert captured["filters"] == {
        "status": "error", "agent_slug": "coder-sonnet",
        "target_slug": "t1", "since_days": 7, "run_id": "run-1",
    }


def test_search_execution_history_no_match_is_an_explicit_miss(monkeypatch):
    from kb_app import exec_pg
    monkeypatch.setattr(exec_pg, "count", lambda: 10)
    monkeypatch.setattr(exec_pg, "search", lambda q, n_results=5, filters=None: [])

    result = _call("search_execution_history", {"query": "nothing like this"})
    assert result["isError"] is True


# ---- diff-zero: search_knowledge_base must never surface executions -------
#
# The card's own words: "diff ZERO no caminho de docs" — search_knowledge_base
# is not allowed to touch the executions table or exec_pg at all. This is the
# single most important test in this feature: it fails loudly the moment
# anyone wires the two together, rather than relying on code review to catch
# a future regression.


def test_search_knowledge_base_never_calls_exec_pg(monkeypatch):
    from kb_app import exec_pg, kb_pg

    monkeypatch.setattr(kb_pg, "count", lambda: 3)
    monkeypatch.setattr(kb_pg, "search", lambda q, n_results=5: [_doc("docs", "a.md", "plain doc content")])

    def _must_not_be_called(*a, **k):
        raise AssertionError("search_knowledge_base must never touch exec_pg")

    monkeypatch.setattr(exec_pg, "search", _must_not_be_called)
    monkeypatch.setattr(exec_pg, "count", _must_not_be_called)

    text = _call("search_knowledge_base", {"query": "anything"})["content"][0]["text"]
    assert "plain doc content" in text


def test_search_execution_history_never_calls_kb_pg_search(monkeypatch):
    """The reverse guarantee: the new tool must not fall back onto the docs
    table either — a bug there would leak `documents` content into a tool
    whose whole point is a separate, execution-only index."""
    from kb_app import exec_pg, kb_pg

    monkeypatch.setattr(exec_pg, "count", lambda: 3)
    monkeypatch.setattr(exec_pg, "search", lambda q, n_results=5, filters=None: [_exec_chunk("run-1")])

    def _must_not_be_called(*a, **k):
        raise AssertionError("search_execution_history must never touch kb_pg.search")

    monkeypatch.setattr(kb_pg, "search", _must_not_be_called)

    result = _call("search_execution_history", {"query": "anything"})
    assert result["isError"] is False
