"""PostgreSQL-backed store for indexed AP-MT execution-run dumps.

A NEW table, ``executions``, in the SAME database/pgvector container as
``kb_pg.py``'s ``documents`` table — never the same table. Design: Architect
run a5b5c488762d4ff98c32d63ca8e1f6ef on Kanban card
3d15bf3b-9510-81fb-9e79-e41107939363 ("Indexar dump de execuções do AP-MT no
aw-app-kb").

Why a separate table, not a row in ``documents``: ``kb_ops._build`` deletes
any ``documents`` row whose source file has disappeared from disk (there is
none here), and ``force=True`` truncates ``documents`` outright — both would
silently wipe execution history. Worse, ``kb_pg.search()`` has no WHERE
clause at all, so a run dump living in ``documents`` would show up in
ordinary ``search_knowledge_base`` results next to real docs. This module
and its callers (``routes.py``'s ``/api/kb/executions/*`` routes,
``mcp_http.py``'s ``search_execution_history`` tool) are the only things
that ever touch ``executions`` — ``kb_pg.py`` is never imported for
anything but its embedding helpers and connection URL, and is never
modified by this feature.

Embedding: reuses ``kb_pg._get_model``/``_embed_docs``/``_embed_query``/
``_vec_str`` rather than loading a second ``TextEmbedding`` — the container
has ``mem_mb: 2048`` and the model alone is ~520 MB.

Chunk size: ``kb_pg._EMBED_MAX_CHARS`` (1500 chars) is where ``_embed_docs``
silently truncates before embedding — a chunk stored past that limit would
report a successful upsert while being semantically invisible to search
past its first ~1500 chars. ``upsert_chunks`` below refuses (raises
``ValueError``) any chunk whose content exceeds that limit, rather than
trust the caller (the agents-platform-runners hook) to have already sliced
it correctly.

Retention: this is an INDEX, not an archive — AP-MT itself never purges
``runs``, so the full record stays reachable via ``get_run_detail`` forever.
Expiring a row here only costs *findability* via ``search_execution_history``,
never data. Two independent limits, both swept by the same ``prune()``:
age (``retention_days``, default 30) and a global row cap
(``max_rows``, default 80000) as a backstop against a burst overrunning the
container's ~2 GB disk budget. Swept once a day from ``main.py``'s own
lifespan — not the ``aw-tasks`` app — so retention can't stop working in
silence just because someone deletes an external scheduled task.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from . import kb_pg as _kb

_log = logging.getLogger(__name__)

# Same bound kb_pg._embed_docs silently truncates under — see module docstring.
_EMBED_MAX_CHARS = _kb._EMBED_MAX_CHARS

_DEFAULT_RETENTION_DAYS = 30
_DEFAULT_MAX_ROWS = 80_000

_conn = None


# ------------------------------------------------------------------
# Connection singleton (lazy, reconnects on error) — mirrors kb_pg.py's
# shape but is its own connection: an error on the executions path resets
# only this connection, not one kb_pg has mid-use elsewhere.
# ------------------------------------------------------------------

def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        import psycopg
        _conn = psycopg.connect(_kb.get_pg_url())
    return _conn


def _reset_conn() -> None:
    global _conn
    try:
        if _conn and not _conn.closed:
            _conn.close()
    except Exception:
        pass
    _conn = None


# ------------------------------------------------------------------
# Schema bootstrap
# ------------------------------------------------------------------

def ensure_exec_schema(retries: int = 12, delay: float = 1.0) -> None:
    """Create the ``executions`` table/indexes if needed.

    Retries so callers can start before Postgres finishes its first-boot
    init. Failures are logged but never raised — mirrors
    ``kb_pg.ensure_kb_schema``.
    """
    for attempt in range(retries):
        try:
            import psycopg
            with psycopg.connect(_kb.get_pg_url(), autocommit=True) as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS executions (
                        chunk_id   TEXT PRIMARY KEY,
                        run_id     TEXT NOT NULL,
                        seq        INT  NOT NULL,
                        content    TEXT NOT NULL,
                        metadata   JSONB NOT NULL DEFAULT '{{}}',
                        indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        embedding  vector({_kb.VECTOR_DIM})
                    )
                """)
                # HNSW: fast approximate NN, works on empty tables.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS executions_embedding_hnsw
                    ON executions USING hnsw (embedding vector_cosine_ops)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS executions_run_id
                    ON executions (run_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS executions_indexed_at
                    ON executions (indexed_at)
                """)
            _log.info("exec_pg: schema ready (%s)", _kb.get_pg_url())
            return
        except Exception as exc:
            _log.warning(
                "exec_pg: schema init attempt %d/%d failed: %s — retrying in %.1fs",
                attempt + 1, retries, exc, delay,
            )
            time.sleep(delay)
    _log.error("exec_pg: schema init failed — pgvector may not be running")


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def count() -> int:
    """Return total number of indexed execution chunks."""
    try:
        row = _get_conn().execute("SELECT COUNT(*) FROM executions").fetchone()
        return row[0] if row else 0
    except Exception:
        _reset_conn()
        return 0


def upsert_chunks(run_id: str, chunks: list[dict[str, Any]]) -> int:
    """Embed and upsert one run's chunks.

    Idempotent AND a full replace: re-indexing the same ``run_id`` overwrites
    its previous chunks rather than appending duplicates, and also deletes any
    old chunk whose ``seq`` is absent from this call (e.g. a re-index with
    fewer chunks than before) — a stale leftover chunk would otherwise sit in
    the index forever, un-owned by any current dump.

    Each chunk is ``{"seq": int, "content": str, "metadata": dict}``.

    Raises
    ------
    ValueError
        If ``run_id`` is empty, ``chunks`` contains an entry missing
        ``seq``/``content``, or any chunk's content exceeds
        ``_EMBED_MAX_CHARS`` — see the module docstring for why this is not
        auto-truncated.
    Exception
        Whatever the DB driver raises, re-raised after ``_reset_conn()`` —
        unlike ``search()``/``count()``/``status()``, which swallow and
        degrade to an empty/zero result, a failed *write* must not report
        success, and the module-global ``_conn`` singleton must not be left
        poisoned for the next caller.
    """
    if not run_id:
        raise ValueError("run_id is required")
    if not chunks:
        return 0

    for c in chunks:
        if c.get("seq") is None or not c.get("content"):
            raise ValueError(f"chunk missing seq/content: {c!r}")

    oversized = [c["seq"] for c in chunks if len(c["content"]) > _EMBED_MAX_CHARS]
    if oversized:
        raise ValueError(
            f"chunk(s) {oversized} for run {run_id!r} exceed {_EMBED_MAX_CHARS} chars "
            f"(kb_pg embeds only the first {_EMBED_MAX_CHARS} chars and reports success "
            f"regardless) — split them smaller before sending"
        )

    texts = [c["content"] for c in chunks]
    vectors = _kb._embed_docs(texts)

    conn = _get_conn()
    try:
        for chunk, vec in zip(chunks, vectors):
            chunk_id = f"{run_id}:{chunk['seq']}"
            vs = _kb._vec_str(vec)
            metadata = dict(chunk.get("metadata") or {})
            conn.execute(
                """
                INSERT INTO executions (chunk_id, run_id, seq, content, metadata, embedding)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::vector)
                ON CONFLICT (chunk_id) DO UPDATE
                    SET content    = EXCLUDED.content,
                        metadata   = EXCLUDED.metadata,
                        embedding  = EXCLUDED.embedding,
                        indexed_at = now()
                """,
                (chunk_id, run_id, chunk["seq"], chunk["content"], json.dumps(metadata), vs),
            )

        seqs = [c["seq"] for c in chunks]
        conn.execute(
            "DELETE FROM executions WHERE run_id = %s AND seq != ALL(%s)",
            (run_id, seqs),
        )
        conn.commit()
    except Exception as exc:
        _log.error("exec_pg: upsert_chunks error for run %s: %s", run_id, exc)
        _reset_conn()
        raise
    return len(chunks)


def delete_run(run_id: str) -> int:
    """Delete every chunk for one run. Returns the number of rows removed."""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM executions WHERE run_id = %s", (run_id,))
    conn.commit()
    return cur.rowcount


def search(query: str, n_results: int = 5, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Cosine-similarity search over indexed execution chunks.

    ``filters`` (all optional, combined with AND, applied in the SQL WHERE —
    never as a post-filter over an overfetch, unlike the docs side's scoped
    search): ``run_id``, ``status``, ``agent_slug``, ``target_slug`` (matched
    against ``metadata->>'...'``), ``since_days`` (matched against
    ``indexed_at``, not ``ended_at`` — see the module docstring's retention
    note on why those two can diverge for a backfilled run).

    Returns a list of dicts: {chunk_id, run_id, seq, content, metadata, score}.
    """
    filters = filters or {}
    where: list[str] = []
    params: list[Any] = []

    if filters.get("run_id"):
        where.append("run_id = %s")
        params.append(filters["run_id"])
    if filters.get("status"):
        where.append("metadata->>'status' = %s")
        params.append(filters["status"])
    if filters.get("agent_slug"):
        where.append("metadata->>'agent_slug' = %s")
        params.append(filters["agent_slug"])
    if filters.get("target_slug"):
        where.append("metadata->>'target_slug' = %s")
        params.append(filters["target_slug"])
    since_days = filters.get("since_days")
    if since_days is not None:
        where.append("indexed_at >= now() - (%s || ' days')::interval")
        params.append(str(int(since_days)))

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    try:
        vs = _kb._vec_str(_kb._embed_query(query))
        rows = _get_conn().execute(
            f"""
            SELECT chunk_id, run_id, seq, content, metadata,
                   1 - (embedding <=> %s::vector) AS score
            FROM   executions
            {where_sql}
            ORDER  BY embedding <=> %s::vector
            LIMIT  %s
            """,
            (vs, *params, vs, n_results),
        ).fetchall()
        return [
            {
                "chunk_id": r[0],
                "run_id": r[1],
                "seq": r[2],
                "content": r[3],
                "metadata": r[4] or {},
                "score": float(r[5]),
            }
            for r in rows
        ]
    except Exception as exc:
        _log.error("exec_pg: search error: %s", exc)
        _reset_conn()
        return []


def prune(retention_days: int | None = None, max_rows: int | None = None) -> dict[str, int]:
    """Delete chunks older than ``retention_days``, then — if the table is
    still over ``max_rows`` — delete the oldest remaining chunks down to that
    cap. ``None`` for either reads the current env var (or this module's
    default) at call time, so an operator changing config doesn't need a
    redeploy for the next daily sweep to pick it up.

    Returns ``{"deleted_by_age": int, "deleted_by_cap": int}``.
    """
    if retention_days is None:
        retention_days = int(os.environ.get("KB_EXEC_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS))
    if max_rows is None:
        max_rows = int(os.environ.get("KB_EXEC_MAX_ROWS", _DEFAULT_MAX_ROWS))

    deleted_by_age = 0
    deleted_by_cap = 0
    try:
        conn = _get_conn()
        cur = conn.execute(
            "DELETE FROM executions WHERE indexed_at < now() - (%s || ' days')::interval",
            (str(int(retention_days)),),
        )
        deleted_by_age = cur.rowcount
        conn.commit()

        if max_rows is not None:
            row = conn.execute("SELECT COUNT(*) FROM executions").fetchone()
            total = row[0] if row else 0
            if total > max_rows:
                excess = total - max_rows
                cur = conn.execute(
                    """
                    DELETE FROM executions WHERE chunk_id IN (
                        SELECT chunk_id FROM executions ORDER BY indexed_at ASC LIMIT %s
                    )
                    """,
                    (excess,),
                )
                deleted_by_cap = cur.rowcount
                conn.commit()
    except Exception as exc:
        _log.error("exec_pg: prune error: %s", exc)
        _reset_conn()

    return {"deleted_by_age": deleted_by_age, "deleted_by_cap": deleted_by_cap}


def status() -> dict[str, Any]:
    """Observability snapshot: chunk/run counts and the indexed_at range."""
    try:
        row = _get_conn().execute(
            "SELECT COUNT(*), COUNT(DISTINCT run_id), MIN(indexed_at), MAX(indexed_at) FROM executions"
        ).fetchone()
        if not row:
            row = (0, 0, None, None)
        return {
            "chunk_count": row[0] or 0,
            "run_count": row[1] or 0,
            "oldest_indexed_at": row[2].isoformat() if row[2] else None,
            "newest_indexed_at": row[3].isoformat() if row[3] else None,
        }
    except Exception as exc:
        _log.error("exec_pg: status error: %s", exc)
        _reset_conn()
        return {"chunk_count": 0, "run_count": 0, "oldest_indexed_at": None, "newest_indexed_at": None}
