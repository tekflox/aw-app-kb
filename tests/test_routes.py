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
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_DATA_DIR", str(tmp_path))
    # Re-import so module-level KB_DIR / settings.DATA_DIR pick up the env
    # var — routes.KB_DIR is imported FROM kb_ops (single source of truth,
    # see routes.py), so kb_ops itself must be reloaded too or it keeps
    # whatever KB_DIR its very first import in this process computed.
    from kb_app import settings as settings_mod
    from kb_app import kb_ops as kb_ops_mod
    from kb_app import routes as routes_mod
    importlib.reload(settings_mod)
    importlib.reload(kb_ops_mod)
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


def test_list_files_honors_kb_dir_override(tmp_path, monkeypatch):
    """Regression: routes.py used to recompute its own KB_DIR from
    settings.DATA_DIR independently of kb_ops.KB_DIR, so files written under
    a KB_DIR_OVERRIDE (the $AW_KB_DIR mount) never showed up in the file
    browser even though build/map correctly indexed them — 764 docs
    indexed, 0 files listed. Reported live 2026-08-05."""
    override = tmp_path / "kb_output"
    override.mkdir()
    (override / "hello.md").write_text("# Hello\n")
    monkeypatch.setenv("KB_DIR_OVERRIDE", str(override))

    from kb_app import settings as settings_mod
    from kb_app import kb_ops as kb_ops_mod
    from kb_app import routes as routes_mod
    importlib.reload(settings_mod)
    importlib.reload(kb_ops_mod)
    importlib.reload(routes_mod)

    app = FastAPI()
    app.include_router(routes_mod.build_routes())
    with TestClient(app) as client:
        res = client.get("/api/kb/files")
    assert res.status_code == 200
    paths = [f["path"] for f in res.json()]
    assert "hello.md" in paths


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
    from kb_app import kb_ops as kb_ops_mod
    from kb_app import routes as routes_mod
    importlib.reload(settings_mod)
    importlib.reload(kb_ops_mod)
    importlib.reload(routes_mod)

    kb_routes = routes_mod.KnowledgeBaseRoutes.__new__(routes_mod.KnowledgeBaseRoutes)
    assert kb_routes._safe_path("../../etc/passwd") is None
    assert kb_routes._safe_path("docs/ok.md") is not None


def test_settings_roundtrip(tmp_path, monkeypatch):
    from kb_app.settings import DEFAULT_MAP_PATHS

    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        # A fresh install is seeded, so the KB indexes something useful
        # without anyone configuring it first.
        res = client.get("/api/kb/settings")
        assert res.json() == {"map_paths": DEFAULT_MAP_PATHS}

        res = client.put("/api/kb/settings", json={"map_paths": [".", "repos/foo"]})
        assert res.json()["map_paths"] == [".", "repos/foo"]

        res = client.get("/api/kb/settings")
        assert res.json()["map_paths"] == [".", "repos/foo"]


def test_seeded_map_paths_can_be_emptied_for_good(tmp_path, monkeypatch):
    """The seed is a DEFAULT, not a floor: clearing it has to stick, or the
    user could never stop indexing the repos tree."""
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.put("/api/kb/settings", json={"map_paths": []})
        assert client.get("/api/kb/settings").json()["map_paths"] == []


def test_list_repos_reflects_repos_dir(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        res = client.get("/api/kb/repos")
        assert res.json() == {"repos": [], "folders": []}

    from kb_app import kb_ops as kb_ops_mod
    os.makedirs(os.path.join(kb_ops_mod.REPOS_DIR, "agentic-workspace"))
    os.makedirs(os.path.join(kb_ops_mod.REPOS_DIR, "another-repo"))
    # A stray file (not a dir) under REPOS_DIR must not show up as a "repo".
    with open(os.path.join(kb_ops_mod.REPOS_DIR, "not-a-repo.txt"), "w") as f:
        f.write("x")

    with TestClient(app) as client:
        res = client.get("/api/kb/repos")
        assert res.json() == {
            "repos": ["agentic-workspace", "another-repo"],
            "folders": [],
        }


def test_list_repos_separates_workspace_mapped_folders(tmp_path, monkeypatch):
    """Mapped folders are reported both in the combined `repos` list (what a
    bare name can resolve to) and on their own, so the UI can distinguish
    "you pointed at this" from "this happens to be cloned under repos/"."""
    app = _make_app(tmp_path, monkeypatch)

    from kb_app import kb_ops as kb_ops_mod
    folders = tmp_path / "workspace-folders"
    os.makedirs(folders / "docs")
    os.makedirs(os.path.join(kb_ops_mod.REPOS_DIR, "some-repo"), exist_ok=True)
    monkeypatch.setattr(kb_ops_mod, "MAPPED_FOLDERS_DIR", str(folders))

    with TestClient(app) as client:
        res = client.get("/api/kb/repos")
        assert res.json() == {"repos": ["docs", "some-repo"], "folders": ["docs"]}


def test_add_repo_starts_a_job(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        res = client.post("/api/kb/add-repo", json={"git_url": "https://example.invalid/x.git"})
        assert res.status_code == 200
        assert res.json()["started"] is True

        res = client.post("/api/kb/add-repo", json={"git_url": ""})
        assert "error" in res.json()


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


def test_translated_host_path_keeps_the_users_name(tmp_path, monkeypatch):
    """A host path is named after ITSELF, not after the mount we find it on.

    /opt/aw-workspace/repos is translated to /workspace-repos in here; taking
    the basename after that showed the folder as "workspace-repos" in the KB,
    a name the user never typed and does not recognise.
    """
    import importlib
    monkeypatch.setenv("KB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")
    mount = tmp_path / "workspace-repos"
    (mount / "some-repo").mkdir(parents=True)
    monkeypatch.setenv("SHARED_REPOS_DIR", str(mount))

    from kb_app import kb_ops
    importlib.reload(kb_ops)

    repo_dir, repo_name, _ = kb_ops._resolve_map_target("/opt/aw-workspace/repos")

    assert repo_name == "repos"
    assert repo_dir == str(mount)
