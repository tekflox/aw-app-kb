"""Route tests for /api/kb/executions* — auth posture and request handling.

Mirrors test_routes.py's style: real FastAPI TestClient, exec_pg (and where
needed kb_pg) monkeypatched so no real Postgres/pgvector or fastembed model
is required.
"""
from __future__ import annotations

import importlib

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_DATA_DIR", str(tmp_path))
    from kb_app import settings as settings_mod
    from kb_app import kb_ops as kb_ops_mod
    from kb_app import routes as routes_mod
    importlib.reload(settings_mod)
    importlib.reload(kb_ops_mod)
    importlib.reload(routes_mod)

    app = FastAPI()
    app.include_router(routes_mod.build_routes())
    return app


def test_post_executions_is_closed_when_secret_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("KB_EXEC_SECRET", raising=False)
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        res = client.post("/api/kb/executions", json={
            "run_id": "r1", "chunks": [{"seq": 0, "content": "x"}],
        })
    assert res.status_code == 503


def test_post_executions_rejects_wrong_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_EXEC_SECRET", "correct-secret")
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        res = client.post(
            "/api/kb/executions",
            json={"run_id": "r1", "chunks": [{"seq": 0, "content": "x"}]},
            headers={"X-KB-Exec-Secret": "wrong"},
        )
    assert res.status_code == 401


def test_post_executions_rejects_missing_secret_header(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_EXEC_SECRET", "correct-secret")
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        res = client.post("/api/kb/executions", json={
            "run_id": "r1", "chunks": [{"seq": 0, "content": "x"}],
        })
    assert res.status_code == 401


def test_post_executions_happy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_EXEC_SECRET", "s3cr3t")
    app = _make_app(tmp_path, monkeypatch)

    from kb_app import exec_pg
    captured = {}

    def _fake_upsert(run_id, chunks):
        captured["run_id"] = run_id
        captured["chunks"] = chunks
        return len(chunks)

    monkeypatch.setattr(exec_pg, "upsert_chunks", _fake_upsert)

    with TestClient(app) as client:
        res = client.post(
            "/api/kb/executions",
            json={
                "run_id": "run-1",
                "metadata_common": {"agent_slug": "coder-sonnet", "target_slug": "t1"},
                "chunks": [
                    {"seq": 0, "content": "summary", "metadata": {"status": "success"}},
                    {"seq": 1, "content": "tool_call detail"},
                ],
            },
            headers={"X-KB-Exec-Secret": "s3cr3t"},
        )

    assert res.status_code == 200
    body = res.json()
    assert body == {"success": True, "run_id": "run-1", "chunks_indexed": 2}
    assert captured["run_id"] == "run-1"
    # metadata_common merged under each chunk's own metadata
    assert captured["chunks"][0]["metadata"] == {
        "agent_slug": "coder-sonnet", "target_slug": "t1", "status": "success",
    }
    assert captured["chunks"][1]["metadata"] == {
        "agent_slug": "coder-sonnet", "target_slug": "t1",
    }


def test_post_executions_requires_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_EXEC_SECRET", "s3cr3t")
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        res = client.post(
            "/api/kb/executions",
            json={"chunks": [{"seq": 0, "content": "x"}]},
            headers={"X-KB-Exec-Secret": "s3cr3t"},
        )
    assert res.status_code == 400


def test_post_executions_requires_nonempty_chunks(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_EXEC_SECRET", "s3cr3t")
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        res = client.post(
            "/api/kb/executions",
            json={"run_id": "r1", "chunks": []},
            headers={"X-KB-Exec-Secret": "s3cr3t"},
        )
    assert res.status_code == 400


def test_post_executions_surfaces_oversized_chunk_as_400(tmp_path, monkeypatch):
    """upsert_chunks' ValueError (oversized/invalid chunk) must reach the
    caller as a 400, not a swallowed 500 or a silent 200."""
    monkeypatch.setenv("KB_EXEC_SECRET", "s3cr3t")
    app = _make_app(tmp_path, monkeypatch)

    from kb_app import exec_pg

    def _boom(run_id, chunks):
        raise ValueError("chunk too big")

    monkeypatch.setattr(exec_pg, "upsert_chunks", _boom)

    with TestClient(app) as client:
        res = client.post(
            "/api/kb/executions",
            json={"run_id": "r1", "chunks": [{"seq": 0, "content": "x"}]},
            headers={"X-KB-Exec-Secret": "s3cr3t"},
        )
    assert res.status_code == 400
    assert "chunk too big" in res.json()["error"]


def test_prune_route_is_also_gated_by_the_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("KB_EXEC_SECRET", raising=False)
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        res = client.post("/api/kb/executions/prune", json={})
    assert res.status_code == 503


def test_prune_route_happy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_EXEC_SECRET", "s3cr3t")
    app = _make_app(tmp_path, monkeypatch)

    from kb_app import exec_pg
    captured = {}

    def _fake_prune(retention_days=None, max_rows=None):
        captured["retention_days"] = retention_days
        captured["max_rows"] = max_rows
        return {"deleted_by_age": 3, "deleted_by_cap": 0}

    monkeypatch.setattr(exec_pg, "prune", _fake_prune)

    with TestClient(app) as client:
        res = client.post(
            "/api/kb/executions/prune",
            json={"retention_days": 0},
            headers={"X-KB-Exec-Secret": "s3cr3t"},
        )
    assert res.status_code == 200
    assert res.json() == {"success": True, "deleted_by_age": 3, "deleted_by_cap": 0}
    assert captured["retention_days"] == 0


def test_status_route_is_public_and_read_only(tmp_path, monkeypatch):
    """Observability endpoint — no secret required, mirrors /api/kb/doc-count."""
    monkeypatch.delenv("KB_EXEC_SECRET", raising=False)
    app = _make_app(tmp_path, monkeypatch)

    from kb_app import exec_pg
    monkeypatch.setattr(exec_pg, "status", lambda: {
        "chunk_count": 12, "run_count": 3,
        "oldest_indexed_at": "2026-08-05T00:00:00", "newest_indexed_at": "2026-09-04T00:00:00",
    })

    with TestClient(app) as client:
        res = client.get("/api/kb/executions/status")
    assert res.status_code == 200
    assert res.json()["chunk_count"] == 12
