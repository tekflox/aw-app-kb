"""Route tests for kb_app/routes.py — file CRUD, search, settings.

kb_pg is monkeypatched so these run with no real Postgres/pgvector and no
fastembed model load (mirrors aw-mcp-gateway's back/tests style: real
FastAPI TestClient, no mocks at the HTTP layer).

routes.py does ``from . import kb_pg`` as a LOCAL import inside each
handler (same lazy-import pattern the original monolith routes used, to
avoid loading fastembed at module-import time) — that always resolves the
same module object from sys.modules, so monkeypatching attributes directly
on the imported ``kb_pg`` module (not on ``routes`` itself) is what
actually takes effect here.
"""
from __future__ import annotations

import importlib

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_DATA_DIR", str(tmp_path))
    # Re-import so module-level KB_DIR / settings.DATA_DIR pick up the env var.
    from kb_app import settings as settings_mod
    from kb_app import routes as routes_mod
    importlib.reload(settings_mod)
    importlib.reload(routes_mod)

    app = FastAPI()
    app.include_router(routes_mod.build_routes())
    return app


def _stub_kb_pg(monkeypatch, **overrides):
    from kb_app import kb_pg as kb_pg_mod
    defaults = {"upsert": lambda **k: None, "delete": lambda *a: True, "count": lambda: 0}
    defaults.update(overrides)
    for name, fn in defaults.items():
        monkeypatch.setattr(kb_pg_mod, name, fn)


def test_list_files_empty(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/api/kb/files")
    assert res.status_code == 200
    assert res.json() == []


def test_save_read_delete_file_roundtrip(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    _stub_kb_pg(monkeypatch)

    with TestClient(app) as client:
        res = client.put("/api/kb/file/docs/hello.md", json={"content": "# Hello\n"})
        assert res.json()["success"] is True

        res = client.get("/api/kb/file/docs/hello.md")
        assert res.json() == {"path": "docs/hello.md", "content": "# Hello\n", "success": True}

        res = client.get("/api/kb/files")
        paths = [f["path"] for f in res.json()]
        assert "docs/hello.md" in paths

        res = client.delete("/api/kb/file/docs/hello.md")
        assert res.json()["success"] is True

        res = client.get("/api/kb/file/docs/hello.md")
        assert res.json()["success"] is False


def test_path_traversal_is_rejected(tmp_path, monkeypatch):
    # httpx (TestClient's transport) normalizes ".." client-side before the
    # request ever reaches the app, so this exercises _safe_path() directly
    # rather than round-tripping through a client that would mask the bug.
    monkeypatch.setenv("KB_DATA_DIR", str(tmp_path))
    from kb_app import settings as settings_mod
    from kb_app import routes as routes_mod
    importlib.reload(settings_mod)
    importlib.reload(routes_mod)

    kb_routes = routes_mod.KnowledgeBaseRoutes.__new__(routes_mod.KnowledgeBaseRoutes)
    assert kb_routes._safe_path("../../etc/passwd") is None
    assert kb_routes._safe_path("docs/ok.md") is not None


def test_settings_roundtrip(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/api/kb/settings")
        assert res.json() == {"map_paths": []}

        res = client.put("/api/kb/settings", json={"map_paths": [".", "repos/foo"]})
        assert res.json()["map_paths"] == [".", "repos/foo"]

        res = client.get("/api/kb/settings")
        assert res.json()["map_paths"] == [".", "repos/foo"]


def test_search_files_matches_content(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    _stub_kb_pg(monkeypatch)
    with TestClient(app) as client:
        client.put("/api/kb/file/docs/needle.md", json={"content": "haystack needle haystack"})
        res = client.get("/api/kb/search", params={"q": "needle"})
        results = res.json()
        assert len(results) == 1
        assert results[0]["path"] == "docs/needle.md"
        assert results[0]["content_match"] is True


def test_doc_count_reflects_kb_pg(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    _stub_kb_pg(monkeypatch, count=lambda: 42)
    with TestClient(app) as client:
        res = client.get("/api/kb/doc-count")
    assert res.json() == {"count": 42}
