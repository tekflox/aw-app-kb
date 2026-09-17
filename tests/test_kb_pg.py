"""Tests for kb_app/kb_pg.py — the pgvector-backed document store.

Same approach as test_exec_pg.py: mock at the connection boundary with a
small fake connection that records executed SQL/params, and stub the
embedding helpers so nothing tries to load the 520 MB fastembed model or
needs a real Postgres.
"""
from __future__ import annotations

import pytest

from kb_app import kb_pg

# Captured before the autouse `_no_real_embedding` fixture below stubs these
# out, so the embed_docs/embed_query prefixing tests can exercise the real
# implementation while everything else in this file never touches fastembed.
_REAL_EMBED_DOCS = kb_pg._embed_docs
_REAL_EMBED_QUERY = kb_pg._embed_query


class _FakeCursor:
    def __init__(self, rows=None, rowcount=0):
        self.rows = rows or []
        self.rowcount = rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _FakeConn:
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
    monkeypatch.setattr(kb_pg, "_get_conn", lambda: conn)
    monkeypatch.setattr(kb_pg, "_conn", conn)
    return conn


@pytest.fixture(autouse=True)
def _no_real_embedding(monkeypatch):
    monkeypatch.setattr(kb_pg, "_embed_docs", lambda texts: [[0.1, 0.2] for _ in texts])
    monkeypatch.setattr(kb_pg, "_embed_query", lambda text: [0.1, 0.2])


# ---------------------------------------------------------------------------
# get_pg_url
# ---------------------------------------------------------------------------

def test_get_pg_url_defaults(monkeypatch):
    monkeypatch.delenv("KB_PG_URL", raising=False)
    assert kb_pg.get_pg_url() == kb_pg._DEFAULT_URL


def test_get_pg_url_honors_env(monkeypatch):
    monkeypatch.setenv("KB_PG_URL", "postgresql://custom")
    assert kb_pg.get_pg_url() == "postgresql://custom"


# ---------------------------------------------------------------------------
# _vec_str
# ---------------------------------------------------------------------------

def test_vec_str_formats_as_pgvector_literal():
    assert kb_pg._vec_str([1.0, 2.5, 0.0]) == "[1,2.5,0]"


# ---------------------------------------------------------------------------
# count
# ---------------------------------------------------------------------------

def test_count_returns_row_value(fake_conn):
    fake_conn.queue_result(rows=[(42,)])
    assert kb_pg.count() == 42


def test_count_returns_zero_when_no_row(fake_conn):
    assert kb_pg.count() == 0


def test_count_swallows_db_errors_and_resets(monkeypatch, fake_conn):
    monkeypatch.setattr(fake_conn, "execute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    reset_calls = []
    monkeypatch.setattr(kb_pg, "_reset_conn", lambda: reset_calls.append(1))
    assert kb_pg.count() == 0
    assert reset_calls == [1]


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def test_search_returns_id_content_metadata_score(fake_conn):
    fake_conn.queue_result(rows=[("doc-1", "hello", {"repo": "x"}, 0.87)])
    results = kb_pg.search("query")
    assert results == [{"id": "doc-1", "content": "hello", "metadata": {"repo": "x"}, "score": 0.87}]


def test_search_defaults_metadata_to_empty_dict_when_null(fake_conn):
    fake_conn.queue_result(rows=[("doc-1", "hello", None, 0.5)])
    results = kb_pg.search("query")
    assert results[0]["metadata"] == {}


def test_search_passes_n_results_as_limit(fake_conn):
    kb_pg.search("query", n_results=3)
    sql, params = fake_conn.executed[0]
    assert params[-1] == 3


def test_search_swallows_errors_and_resets(monkeypatch, fake_conn):
    monkeypatch.setattr(fake_conn, "execute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    reset_calls = []
    monkeypatch.setattr(kb_pg, "_reset_conn", lambda: reset_calls.append(1))
    assert kb_pg.search("query") == []
    assert reset_calls == [1]


# ---------------------------------------------------------------------------
# upsert / upsert_many
# ---------------------------------------------------------------------------

def test_upsert_issues_insert_on_conflict_and_commits(fake_conn):
    kb_pg.upsert("doc-1", "content", {"repo": "x"})
    sql, params = fake_conn.executed[0]
    assert "INSERT INTO documents" in sql
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert params[0] == "doc-1"
    assert params[1] == "content"
    assert fake_conn.committed == 1


def test_upsert_many_empty_list_is_a_noop(fake_conn):
    assert kb_pg.upsert_many([]) == 0
    assert fake_conn.executed == []


def test_upsert_many_writes_each_doc_and_commits_once(fake_conn):
    docs = [("d1", "c1", {"a": 1}), ("d2", "c2", {"b": 2})]
    n = kb_pg.upsert_many(docs)
    assert n == 2
    inserts = [e for e in fake_conn.executed if e[0].strip().startswith("INSERT")]
    assert len(inserts) == 2
    assert fake_conn.committed == 1


# ---------------------------------------------------------------------------
# delete / delete_many
# ---------------------------------------------------------------------------

def test_delete_returns_true_when_row_existed(fake_conn):
    fake_conn.queue_result(rowcount=1)
    assert kb_pg.delete("doc-1") is True
    sql, params = fake_conn.executed[0]
    assert "DELETE FROM documents WHERE id = %s" == sql.strip()
    assert params == ("doc-1",)


def test_delete_returns_false_when_nothing_deleted(fake_conn):
    fake_conn.queue_result(rowcount=0)
    assert kb_pg.delete("doc-1") is False


def test_delete_many_empty_list_is_a_noop(fake_conn):
    assert kb_pg.delete_many([]) == 0
    assert fake_conn.executed == []


def test_delete_many_returns_rowcount(fake_conn):
    fake_conn.queue_result(rowcount=3)
    n = kb_pg.delete_many(["a", "b", "c"])
    assert n == 3
    sql, params = fake_conn.executed[0]
    assert "= ANY(%s)" in sql
    assert params == (["a", "b", "c"],)


# ---------------------------------------------------------------------------
# get_all_metadata
# ---------------------------------------------------------------------------

def test_get_all_metadata_returns_id_to_metadata_map(fake_conn):
    fake_conn.queue_result(rows=[("d1", {"a": 1}), ("d2", None)])
    result = kb_pg.get_all_metadata()
    assert result == {"d1": {"a": 1}, "d2": {}}


def test_get_all_metadata_swallows_errors_and_resets(monkeypatch, fake_conn):
    monkeypatch.setattr(fake_conn, "execute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    reset_calls = []
    monkeypatch.setattr(kb_pg, "_reset_conn", lambda: reset_calls.append(1))
    assert kb_pg.get_all_metadata() == {}
    assert reset_calls == [1]


# ---------------------------------------------------------------------------
# _reset_conn / _get_conn
# ---------------------------------------------------------------------------

def test_reset_conn_closes_open_connection(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(kb_pg, "_conn", conn)
    kb_pg._reset_conn()
    assert conn.closed is True
    assert kb_pg._conn is None


def test_reset_conn_swallows_close_errors(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(conn, "close", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(kb_pg, "_conn", conn)
    kb_pg._reset_conn()  # must not raise
    assert kb_pg._conn is None


def test_reset_conn_noop_when_no_connection(monkeypatch):
    monkeypatch.setattr(kb_pg, "_conn", None)
    kb_pg._reset_conn()
    assert kb_pg._conn is None


def test_get_conn_reuses_open_connection(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(kb_pg, "_conn", conn)
    assert kb_pg._get_conn() is conn


def test_get_conn_reconnects_when_none(monkeypatch):
    monkeypatch.setattr(kb_pg, "_conn", None)
    created = _FakeConn()
    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda url: created)
    assert kb_pg._get_conn() is created
    assert kb_pg._conn is created


def test_get_conn_reconnects_when_closed(monkeypatch):
    old = _FakeConn()
    old.closed = True
    monkeypatch.setattr(kb_pg, "_conn", old)
    created = _FakeConn()
    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda url: created)
    assert kb_pg._get_conn() is created


# ---------------------------------------------------------------------------
# ensure_kb_schema
# ---------------------------------------------------------------------------

def test_ensure_kb_schema_creates_extension_table_and_index(monkeypatch):
    executed = []

    class _SchemaConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, *a, **k):
            executed.append(sql)

    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _SchemaConn())

    kb_pg.ensure_kb_schema(retries=1, delay=0)

    assert any("CREATE EXTENSION IF NOT EXISTS vector" in s for s in executed)
    assert any("CREATE TABLE IF NOT EXISTS documents" in s for s in executed)
    assert any("CREATE INDEX IF NOT EXISTS documents_embedding_hnsw" in s for s in executed)


def test_ensure_kb_schema_retries_then_gives_up_quietly(monkeypatch):
    import psycopg
    attempts = []

    def _boom(*a, **k):
        attempts.append(1)
        raise RuntimeError("no db")

    monkeypatch.setattr(psycopg, "connect", _boom)

    kb_pg.ensure_kb_schema(retries=3, delay=0)  # must not raise

    assert len(attempts) == 3


# ---------------------------------------------------------------------------
# _get_model / _embed_docs / _embed_query — real (non-mocked) behaviour of
# the prefixing/truncation logic, with TextEmbedding itself stubbed out.
# ---------------------------------------------------------------------------

def test_embed_docs_prefixes_and_truncates(monkeypatch):
    captured = {}

    class _FakeModel:
        def embed(self, texts, batch_size=1):
            captured["texts"] = texts
            captured["batch_size"] = batch_size
            return [[0.1, 0.2] for _ in texts]

    monkeypatch.setattr(kb_pg, "_model", _FakeModel())

    long_text = "y" * (kb_pg._EMBED_MAX_CHARS + 50)
    result = _REAL_EMBED_DOCS([long_text])

    assert captured["texts"][0].startswith("search_document: ")
    assert len(captured["texts"][0]) == len("search_document: ") + kb_pg._EMBED_MAX_CHARS
    assert captured["batch_size"] == 1
    assert result == [[0.1, 0.2]]


def test_embed_query_prefixes_with_search_query(monkeypatch):
    captured = {}

    class _FakeModel:
        def embed(self, texts):
            captured["texts"] = texts
            return [[0.3, 0.4]]

    monkeypatch.setattr(kb_pg, "_model", _FakeModel())

    result = _REAL_EMBED_QUERY("how do I configure X")

    assert captured["texts"] == ["search_query: how do I configure X"]
    assert result == [0.3, 0.4]


def test_get_model_lazily_creates_and_caches(monkeypatch):
    created = []

    class _FakeTextEmbedding:
        def __init__(self, name, threads=1):
            created.append((name, threads))

    import fastembed
    monkeypatch.setattr(fastembed, "TextEmbedding", _FakeTextEmbedding)
    monkeypatch.setattr(kb_pg, "_model", None)

    m1 = kb_pg._get_model()
    m2 = kb_pg._get_model()

    assert m1 is m2
    assert len(created) == 1
    assert created[0][0] == kb_pg._MODEL_NAME
