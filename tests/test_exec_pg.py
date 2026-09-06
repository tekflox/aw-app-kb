"""Tests for kb_app/exec_pg.py — the AP-MT execution-history index.

No real Postgres in CI (same constraint kb_pg.py's own tests live under —
see test_routes.py/test_mcp_http.py, which never touch a real database
either): these mock at the connection boundary with a small fake connection
that records executed SQL/params, and stub kb_pg's embedding helpers so
nothing tries to load the 520 MB fastembed model.
"""
from __future__ import annotations

import inspect

import pytest

from kb_app import exec_pg, kb_pg


class _FakeCursor:
    def __init__(self, rows=None, rowcount=0):
        self.rows = rows or []
        self.rowcount = rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _FakeConn:
    """Records every executed statement; lets a test queue canned results."""

    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self.closed = False
        self.committed = 0
        self._queue: list[_FakeCursor] = []

    def queue_result(self, rows=None, rowcount=0):
        self._queue.append(_FakeCursor(rows, rowcount))

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if self._queue:
            return self._queue.pop(0)
        return _FakeCursor()

    def commit(self):
        self.committed += 1

    def close(self):
        self.closed = True


@pytest.fixture
def fake_conn(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(exec_pg, "_get_conn", lambda: conn)
    monkeypatch.setattr(exec_pg, "_conn", conn)
    return conn


@pytest.fixture(autouse=True)
def _no_real_embedding(monkeypatch):
    """upsert_chunks/search call kb_pg's embedding helpers — stub them so no
    test needs fastembed installed or a model download."""
    monkeypatch.setattr(kb_pg, "_embed_docs", lambda texts: [[0.1, 0.2] for _ in texts])
    monkeypatch.setattr(kb_pg, "_embed_query", lambda text: [0.1, 0.2])


# ------------------------------------------------------------------
# upsert_chunks
# ------------------------------------------------------------------

def test_upsert_chunks_rejects_missing_run_id(fake_conn):
    with pytest.raises(ValueError):
        exec_pg.upsert_chunks("", [{"seq": 0, "content": "x"}])
    assert fake_conn.executed == []


def test_upsert_chunks_empty_list_is_a_noop(fake_conn):
    assert exec_pg.upsert_chunks("run-1", []) == 0
    assert fake_conn.executed == []


def test_upsert_chunks_rejects_chunk_missing_fields(fake_conn):
    with pytest.raises(ValueError):
        exec_pg.upsert_chunks("run-1", [{"seq": 0}])
    assert fake_conn.executed == []


def test_upsert_chunks_rejects_oversized_content_without_touching_the_db(fake_conn):
    """The invariant the Architect flagged: kb_pg silently truncates at
    _EMBED_MAX_CHARS before embedding and still reports success — exec_pg
    must refuse instead of storing a chunk that is search-invisible past
    that point."""
    huge = "x" * (exec_pg._EMBED_MAX_CHARS + 1)
    with pytest.raises(ValueError, match=r"exceed \d+ chars"):
        exec_pg.upsert_chunks("run-1", [{"seq": 0, "content": huge}])
    assert fake_conn.executed == []  # rejected before any SQL ran


def test_upsert_chunks_accepts_content_exactly_at_the_limit(fake_conn):
    ok = "x" * exec_pg._EMBED_MAX_CHARS
    n = exec_pg.upsert_chunks("run-1", [{"seq": 0, "content": ok}])
    assert n == 1


def test_upsert_chunks_writes_composite_chunk_id_and_replaces_stale_seqs(fake_conn):
    exec_pg.upsert_chunks("run-42", [
        {"seq": 0, "content": "summary", "metadata": {"status": "success"}},
        {"seq": 1, "content": "tool_call detail"},
    ])

    inserts = [e for e in fake_conn.executed if e[0].strip().startswith("INSERT")]
    assert len(inserts) == 2
    assert inserts[0][1][0] == "run-42:0"
    assert inserts[1][1][0] == "run-42:1"

    cleanup = [e for e in fake_conn.executed if "DELETE FROM executions WHERE run_id" in e[0]]
    assert len(cleanup) == 1
    sql, params = cleanup[0]
    assert "seq != ALL" in sql
    assert params == ("run-42", [0, 1])

    assert fake_conn.committed >= 1


def test_upsert_chunks_resets_connection_and_reraises_on_db_error(monkeypatch, fake_conn):
    """Unlike search/count/prune/status, a failed WRITE must not be
    swallowed — the caller (routes.py) needs to know the upsert didn't
    happen, and the poisoned module-global connection must not be left for
    the next unrelated run's upsert_chunks call to inherit."""
    def _boom(*a, **k):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(fake_conn, "execute", _boom)
    reset_calls = []
    monkeypatch.setattr(exec_pg, "_reset_conn", lambda: reset_calls.append(1))

    with pytest.raises(RuntimeError, match="connection reset"):
        exec_pg.upsert_chunks("run-1", [{"seq": 0, "content": "x"}])
    assert reset_calls == [1]


def test_upsert_chunks_merges_metadata_into_json(fake_conn):
    import json
    exec_pg.upsert_chunks("run-9", [{"seq": 0, "content": "x", "metadata": {"a": 1}}])
    insert_sql, insert_params = next(e for e in fake_conn.executed if e[0].strip().startswith("INSERT"))
    metadata_json = insert_params[4]
    assert json.loads(metadata_json) == {"a": 1}


# ------------------------------------------------------------------
# delete_run
# ------------------------------------------------------------------

def test_delete_run_issues_a_scoped_delete(fake_conn):
    fake_conn.queue_result(rowcount=7)
    n = exec_pg.delete_run("run-1")
    assert n == 7
    sql, params = fake_conn.executed[0]
    assert "DELETE FROM executions WHERE run_id = %s" == sql.strip()
    assert params == ("run-1",)


# ------------------------------------------------------------------
# search — filters land in the SQL WHERE clause
# ------------------------------------------------------------------

def test_search_with_no_filters_has_no_where_clause(fake_conn):
    exec_pg.search("query text")
    sql, params = fake_conn.executed[0]
    assert "WHERE" not in sql


def test_search_filters_compose_into_where_and(fake_conn):
    exec_pg.search("query text", n_results=3, filters={
        "run_id": "run-1", "status": "error", "agent_slug": "coder-sonnet",
        "target_slug": "t1", "since_days": 7,
    })
    sql, params = fake_conn.executed[0]
    assert "run_id = %s" in sql
    assert "metadata->>'status' = %s" in sql
    assert "metadata->>'agent_slug' = %s" in sql
    assert "metadata->>'target_slug' = %s" in sql
    assert "indexed_at >= now()" in sql
    assert " AND " in sql
    # order: vec, run_id, status, agent_slug, target_slug, since_days, vec, n_results
    assert params[1] == "run-1"
    assert params[2] == "error"
    assert params[-1] == 3


def test_search_returns_score_and_fields(fake_conn):
    fake_conn.queue_result(rows=[
        ("run-1:0", "run-1", 0, "matched text", {"status": "success"}, 0.87),
    ])
    results = exec_pg.search("x")
    assert results == [{
        "chunk_id": "run-1:0", "run_id": "run-1", "seq": 0,
        "content": "matched text", "metadata": {"status": "success"}, "score": 0.87,
    }]


def test_search_swallows_db_errors_and_resets_connection(monkeypatch, fake_conn):
    def _boom(*a, **k):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(fake_conn, "execute", _boom)
    reset_calls = []
    monkeypatch.setattr(exec_pg, "_reset_conn", lambda: reset_calls.append(1))

    assert exec_pg.search("x") == []
    assert reset_calls == [1]


# ------------------------------------------------------------------
# prune
# ------------------------------------------------------------------

def test_prune_deletes_by_age_only_when_under_the_cap(fake_conn):
    fake_conn.queue_result(rowcount=5)   # DELETE ... indexed_at < ...
    fake_conn.queue_result(rows=[(10,)])  # SELECT COUNT(*)

    result = exec_pg.prune(retention_days=30, max_rows=100)

    assert result == {"deleted_by_age": 5, "deleted_by_cap": 0}
    age_delete = fake_conn.executed[0]
    assert "indexed_at < now()" in age_delete[0]
    assert age_delete[1] == ("30",)


def test_prune_also_deletes_oldest_rows_when_over_the_cap(fake_conn):
    fake_conn.queue_result(rowcount=0)    # DELETE by age
    fake_conn.queue_result(rows=[(120,)])  # SELECT COUNT(*) -> over cap
    fake_conn.queue_result(rowcount=20)   # DELETE oldest 20

    result = exec_pg.prune(retention_days=30, max_rows=100)

    assert result == {"deleted_by_age": 0, "deleted_by_cap": 20}
    cap_delete_sql, cap_delete_params = fake_conn.executed[-1]
    assert "ORDER BY indexed_at ASC LIMIT %s" in cap_delete_sql
    assert cap_delete_params == (20,)


def test_prune_reads_env_defaults_when_args_omitted(monkeypatch, fake_conn):
    monkeypatch.setenv("KB_EXEC_RETENTION_DAYS", "14")
    monkeypatch.setenv("KB_EXEC_MAX_ROWS", "5")
    fake_conn.queue_result(rowcount=0)
    fake_conn.queue_result(rows=[(0,)])

    exec_pg.prune()

    age_delete = fake_conn.executed[0]
    assert age_delete[1] == ("14",)


# ------------------------------------------------------------------
# status / count
# ------------------------------------------------------------------

def test_status_reports_counts_and_range(fake_conn):
    import datetime
    now = datetime.datetime(2026, 9, 4, tzinfo=datetime.timezone.utc)
    fake_conn.queue_result(rows=[(42, 6, now, now)])
    result = exec_pg.status()
    assert result["chunk_count"] == 42
    assert result["run_count"] == 6
    assert result["oldest_indexed_at"] == now.isoformat()


def test_status_degrades_to_zeros_on_error(monkeypatch, fake_conn):
    monkeypatch.setattr(fake_conn, "execute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    result = exec_pg.status()
    assert result == {
        "chunk_count": 0, "run_count": 0, "oldest_indexed_at": None, "newest_indexed_at": None,
    }


def test_count_degrades_to_zero_on_error(monkeypatch, fake_conn):
    monkeypatch.setattr(fake_conn, "execute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert exec_pg.count() == 0


# ------------------------------------------------------------------
# THE most important invariant: this feature never touches documents/
# search_knowledge_base's path. See kb_app/exec_pg.py's module docstring
# and the card's "Invariante inegociável — diff ZERO no caminho de docs".
# ------------------------------------------------------------------

def test_kb_pg_source_never_mentions_the_executions_table():
    """kb_pg.py must be untouched by this feature — grepping its own source
    for the new table name is a cheap, durable tripwire against a future
    edit that starts blending the two."""
    src = inspect.getsource(kb_pg)
    assert "executions" not in src


def test_kb_ops_force_build_truncate_never_touches_executions(tmp_path, monkeypatch):
    """kb_ops._build(force=True) TRUNCATEs `documents` — assert that SQL
    literally never mentions `executions`, so a rebuild from the Manage
    panel can never wipe execution history."""
    from kb_app import kb_ops
    import importlib
    monkeypatch.setenv("KB_DATA_DIR", str(tmp_path))
    importlib.reload(kb_ops)

    monkeypatch.setattr(kb_pg, "ensure_kb_schema", lambda **k: None)
    monkeypatch.setattr(kb_pg, "get_all_metadata", lambda: {})
    monkeypatch.setattr(kb_pg, "upsert_many", lambda docs: len(docs))
    monkeypatch.setattr(kb_pg, "delete_many", lambda ids: 0)
    monkeypatch.setattr(kb_pg, "count", lambda: 0)

    executed = []

    class _FakeSchemaConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, *a, **k):
            executed.append(sql)

    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _FakeSchemaConn())

    kb_ops._build(force=True)

    assert any("TRUNCATE TABLE documents" in s for s in executed)
    assert not any("executions" in s for s in executed)
