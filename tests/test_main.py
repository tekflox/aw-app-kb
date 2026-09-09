"""Tests for kb_app/main.py — the FastAPI entrypoint (0% before this pilot
card). lifespan()'s real Postgres schema calls and self-registration are
monkeypatched out; the point of these tests is the routes main.py wires up
itself (/healthz, /mcp), not kb_pg/exec_pg/self_register, which have their
own test files.
"""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def _make_app(monkeypatch):
    from kb_app import main as main_mod
    from kb_app import kb_pg as kb_pg_mod
    from kb_app import exec_pg as exec_pg_mod
    from kb_app import self_register as self_register_mod

    monkeypatch.setattr(kb_pg_mod, "ensure_kb_schema", lambda retries, delay: None)
    monkeypatch.setattr(kb_pg_mod, "count", lambda: 3)
    monkeypatch.setattr(exec_pg_mod, "ensure_exec_schema", lambda retries, delay: None)
    monkeypatch.setattr(self_register_mod, "register_self", lambda port: None)

    importlib.reload(main_mod)
    return main_mod.build_app()


def test_healthz_reports_doc_count(monkeypatch):
    app = _make_app(monkeypatch)
    with TestClient(app) as client:
        res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"ok": True, "docs": 3}


def test_mcp_get_is_not_allowed(monkeypatch):
    app = _make_app(monkeypatch)
    with TestClient(app) as client:
        res = client.get("/mcp")
    assert res.status_code == 405


def test_mcp_post_single_message(monkeypatch):
    app = _make_app(monkeypatch)
    with TestClient(app) as client:
        res = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        )
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == 1
    assert body["result"]["serverInfo"]["name"] == "aw-app-kb"


def test_mcp_post_notification_returns_202(monkeypatch):
    app = _make_app(monkeypatch)
    with TestClient(app) as client:
        res = client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
    assert res.status_code == 202


def test_mcp_post_batch(monkeypatch):
    app = _make_app(monkeypatch)
    with TestClient(app) as client:
        res = client.post(
            "/mcp",
            json=[
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "id": 2, "method": "initialize"},
            ],
        )
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body, list)
    assert [b["id"] for b in body] == [1, 2]
