"""PostgreSQL + pgvector backend for the Knowledge Base.

Replaces ChromaDB's SQLite-backed storage with a proper PostgreSQL vector
store using the pgvector extension and cosine similarity (<=> operator).

Embedding model
---------------
``nomic-ai/nomic-embed-text-v1.5`` via ``fastembed`` (ONNX runtime — no PyTorch).

* 768 dimensions  (vs 384 for the old all-MiniLM-L6-v2)
* 8192-token context  (vs 256 — no more silent truncation of long docs)
* Task-prefix aware:
    - Indexing  → ``"search_document: <text>"``
    - Querying  → ``"search_query: <text>"``
* Model cached at ``$FASTEMBED_CACHE_PATH`` (default ``/tmp/fastembed_cache``) after first download (~520 MB).
* Inputs are truncated to ``_EMBED_MAX_CHARS`` (1 500 chars ≈ 400 tokens) before embedding
  to bound the O(n²) attention memory and prevent OOM on constrained hosts.

Connection
----------
Ported from agentic-workspace's src/libs/kb_pg.py, which talked to a
SEPARATE aw-pgvector sibling container on port 5433. Here Postgres is
bundled into this app's own container (see entrypoint.sh) as a plain local
process, so the default moves to the standard port 5432 on localhost.
Reads ``KB_PG_URL`` env var; defaults to:
    postgresql://postgres:postgres@127.0.0.1:5432/knowledge_base

Schema (auto-created on first use)
------------------------------------
    CREATE EXTENSION IF NOT EXISTS vector;

    CREATE TABLE documents (
        id        TEXT PRIMARY KEY,
        content   TEXT NOT NULL,
        metadata  JSONB NOT NULL DEFAULT '{}',
        embedding vector(768)
    );

    CREATE INDEX documents_embedding_hnsw
    ON documents USING hnsw (embedding vector_cosine_ops);
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

_log = logging.getLogger(__name__)

_DEFAULT_URL = "postgresql://postgres:postgres@127.0.0.1:5432/knowledge_base"
VECTOR_DIM = 768          # nomic-embed-text-v1.5
_MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"   # float32, 0.52 GB, 768 dims, 8192-token ctx

# nomic has 8192-token context but O(n²) attention memory.  Capping input at
# 1 500 chars (≈400 tokens) bounds the per-document peak RSS to ~500 MB,
# keeping the full build well within a 15 GB container.  The KB code-map
# documents are structured markdown; the most searchable content (names,
# signatures, docstrings) is always near the top, so truncation is safe.
_EMBED_MAX_CHARS = 1500

_model = None
_conn = None


# ------------------------------------------------------------------
# Connection URL
# ------------------------------------------------------------------

def get_pg_url() -> str:
    """Return the psycopg3 connection URL for the knowledge-base database."""
    return os.environ.get("KB_PG_URL", _DEFAULT_URL)


# ------------------------------------------------------------------
# Embeddings  — nomic-embed-text-v1.5 via fastembed (ONNX, no PyTorch)
# ------------------------------------------------------------------

def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        # threads=2: ONNX intra-op parallelism across the 4 available cores.
        # Provides ~20-40% faster per-document embedding vs threads=1 with
        # negligible memory overhead.  We previously used threads=1 to guard
        # against OOM during TextEmbedding init, but that was caused by
        # quadratic attention on long inputs — now fixed by _EMBED_MAX_CHARS
        # truncation, so threads=2 is safe.
        _model = TextEmbedding(_MODEL_NAME, threads=2)
    return _model


def _embed_docs(texts: list[str]) -> list[list[float]]:
    """Embed texts for **indexing** (search_document: prefix).

    nomic-embed-text-v1.5 is task-prefix aware: documents and queries live
    in different sub-spaces; using the correct prefix improves recall.

    Memory note: nomic's 8192-token context uses O(n²) attention.  Long
    inputs cause attention matrices to grow quadratically and can OOM on
    machines with limited free swap.  We truncate to _EMBED_MAX_CHARS before
    prefixing, which caps seq_len at roughly 400 tokens and keeps peak RSS
    well under 500 MB even for batches of 16.
    """
    model = _get_model()
    prefixed = ["search_document: " + t[:_EMBED_MAX_CHARS] for t in texts]
    # batch_size=1 prevents ONNX from padding all seqs to the longest in the
    # batch, which would multiply the memory overhead by the batch size.
    return [list(float(x) for x in v) for v in model.embed(prefixed, batch_size=1)]


def _embed_query(text: str) -> list[float]:
    """Embed a **search query** (search_query: prefix)."""
    model = _get_model()
    vecs = list(model.embed(["search_query: " + text]))
    return [float(x) for x in vecs[0]]


def _vec_str(vec: list[float]) -> str:
    """Serialise a float list to pgvector literal: [1.0,2.0,...]."""
    return "[" + ",".join(f"{x:.8g}" for x in vec) + "]"


# ------------------------------------------------------------------
# Connection singleton (lazy, reconnects on error)
# ------------------------------------------------------------------

def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        import psycopg
        _conn = psycopg.connect(get_pg_url())
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

def ensure_kb_schema(retries: int = 12, delay: float = 1.0) -> None:
    """Create the pgvector extension and documents table if needed.

    Retries so callers can start before aw-pgvector finishes its first-boot
    init.  Failures are logged but never raised — the server stays up even
    when aw-pgvector is offline, and will reconnect on the next operation.
    """
    for attempt in range(retries):
        try:
            import psycopg
            with psycopg.connect(get_pg_url(), autocommit=True) as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS documents (
                        id        TEXT PRIMARY KEY,
                        content   TEXT NOT NULL,
                        metadata  JSONB NOT NULL DEFAULT '{{}}',
                        embedding vector({VECTOR_DIM})
                    )
                """)
                # HNSW: fast approximate NN, works on empty tables,
                # no minimum-row requirement unlike IVFFlat.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS documents_embedding_hnsw
                    ON documents USING hnsw (embedding vector_cosine_ops)
                """)
            _log.info("kb_pg: schema ready (aw-pgvector @ %s, model=%s)", get_pg_url(), _MODEL_NAME)
            return
        except Exception as exc:
            _log.warning(
                "kb_pg: schema init attempt %d/%d failed: %s — retrying in %.1fs",
                attempt + 1, retries, exc, delay,
            )
            time.sleep(delay)
    _log.error("kb_pg: schema init failed — aw-pgvector may not be running")


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def count() -> int:
    """Return total number of documents in the store."""
    try:
        row = _get_conn().execute("SELECT COUNT(*) FROM documents").fetchone()
        return row[0] if row else 0
    except Exception:
        _reset_conn()
        return 0


def search(query: str, n_results: int = 5) -> list[dict[str, Any]]:
    """Cosine-similarity search over embedded documents.

    Returns a list of dicts: {id, content, metadata, score}
    where score is in [0, 1] (1 = identical).
    """
    try:
        vs = _vec_str(_embed_query(query))
        rows = _get_conn().execute(
            """
            SELECT id, content, metadata,
                   1 - (embedding <=> %s::vector) AS score
            FROM   documents
            ORDER  BY embedding <=> %s::vector
            LIMIT  %s
            """,
            (vs, vs, n_results),
        ).fetchall()
        return [
            {
                "id": r[0],
                "content": r[1],
                "metadata": r[2] or {},
                "score": float(r[3]),
            }
            for r in rows
        ]
    except Exception as exc:
        _log.error("kb_pg: search error: %s", exc)
        _reset_conn()
        return []


def upsert(doc_id: str, content: str, metadata: dict) -> None:
    """Embed and upsert one document (INSERT … ON CONFLICT DO UPDATE)."""
    vs = _vec_str(_embed_docs([content])[0])
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO documents (id, content, metadata, embedding)
        VALUES (%s, %s, %s::jsonb, %s::vector)
        ON CONFLICT (id) DO UPDATE
            SET content   = EXCLUDED.content,
                metadata  = EXCLUDED.metadata,
                embedding = EXCLUDED.embedding
        """,
        (doc_id, content, json.dumps(metadata), vs),
    )
    conn.commit()


def upsert_many(docs: list[tuple[str, str, dict]]) -> int:
    """Batch embed + upsert.

    Parameters
    ----------
    docs:
        List of ``(doc_id, content, metadata)`` tuples.

    Returns
    -------
    int
        Number of rows processed.
    """
    if not docs:
        return 0
    texts = [d[1] for d in docs]
    vectors = _embed_docs(texts)
    conn = _get_conn()
    for (doc_id, content, metadata), vec in zip(docs, vectors):
        vs = _vec_str(vec)
        conn.execute(
            """
            INSERT INTO documents (id, content, metadata, embedding)
            VALUES (%s, %s, %s::jsonb, %s::vector)
            ON CONFLICT (id) DO UPDATE
                SET content   = EXCLUDED.content,
                    metadata  = EXCLUDED.metadata,
                    embedding = EXCLUDED.embedding
            """,
            (doc_id, content, json.dumps(metadata), vs),
        )
    conn.commit()
    return len(docs)


def delete(doc_id: str) -> bool:
    """Delete one document by id. Returns True if it existed."""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
    conn.commit()
    return bool(cur.rowcount)


def delete_many(doc_ids: list[str]) -> int:
    """Delete multiple documents at once. Returns count actually deleted."""
    if not doc_ids:
        return 0
    conn = _get_conn()
    cur = conn.execute(
        "DELETE FROM documents WHERE id = ANY(%s)", (list(doc_ids),)
    )
    conn.commit()
    return cur.rowcount


def get_all_metadata() -> dict[str, dict]:
    """Return {doc_id: metadata} for every document.

    Used by ``./aw knowledge-base --build`` to skip unchanged documents.
    """
    try:
        rows = _get_conn().execute(
            "SELECT id, metadata FROM documents"
        ).fetchall()
        return {r[0]: r[1] or {} for r in rows}
    except Exception:
        _reset_conn()
        return {}
