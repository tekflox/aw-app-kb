"""Knowledge Base file browser + editor API.

Ported from agentic-workspace's src/api/routes/knowledge_base.py. Uses
kb_pg (pgvector) for semantic operations. Background job runner for
build/map operations runs as a subprocess to avoid loading the 520 MB
fastembed model into the API process (same reasoning as the original).
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import JSONResponse

from . import settings as _settings
from .kb_ops import KB_DIR

log = logging.getLogger(__name__)

# One level above kb_app/ — where this package's own subprocess re-imports
# itself from (cwd for the background build/map jobs below).
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# KB_DIR imported from kb_ops (single source of truth — this used to be
# recomputed here independently as DATA_DIR/knowledge_base, which went
# stale the moment kb_ops.KB_DIR started honoring KB_DIR_OVERRIDE: the file
# browser kept scanning the old, now-empty directory while --build/--map
# correctly wrote to the new $AW_KB_DIR mount, so the Files panel showed
# empty despite "N docs indexed" being accurate. Reported live 2026-08-05.)

# ---------------------------------------------------------------------------
# Background job state (module-level singleton)
# ---------------------------------------------------------------------------

_job_lock = threading.Lock()
_job_state: dict = {
    "running": False,
    "operation": None,
    "output": [],
    "error": None,
    "last_run": None,
}


# ---------------------------------------------------------------------------
# File-listing cache
# ---------------------------------------------------------------------------
# /api/kb/files walks the WHOLE knowledge base and stats every file — ~9.9k
# entries / ~1.5 MB of JSON on this install. The UI polls it, so an idle KB
# tab was paying that walk plus that payload every 10 s, forever. Two things
# fix it without changing the response shape any caller depends on:
#
#   * a short TTL cache, so N clients (or one client polling fast during a
#     build) share a single walk rather than each triggering their own;
#   * a strong-ish ETag over (path, size, mtime), so an unchanged tree costs
#     a 304 with an empty body instead of the full list.
#
# Writes go through save_file/delete_file, which invalidate explicitly — the
# TTL is a backstop for changes made underneath us (a build/map job, an agent
# calling update_knowledge_base), not the primary freshness mechanism.
_FILES_CACHE_TTL = 5.0
_files_cache_lock = threading.Lock()
_files_cache: dict = {"key": None, "at": 0.0, "etag": "", "entries": []}


def _scan_files() -> list[dict]:
    result = []
    for root, dirs, files in os.walk(KB_DIR):
        dirs.sort()
        for f in sorted(files):
            if f.startswith("."):
                continue
            full_path = os.path.join(root, f)
            rel = os.path.relpath(full_path, KB_DIR)
            try:
                stat = os.stat(full_path)
            except OSError:
                # Raced with a build job rewriting the tree — skip it rather
                # than 500 the whole listing.
                continue
            result.append({
                "path": rel,
                "name": f,
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })
    return result


def _files_snapshot() -> tuple[str, list[dict]]:
    """Return (etag, entries), reusing a recent scan when one exists."""
    now = time.monotonic()
    with _files_cache_lock:
        if _files_cache["key"] == KB_DIR and now - _files_cache["at"] < _FILES_CACHE_TTL:
            return _files_cache["etag"], _files_cache["entries"]

    entries = _scan_files()
    digest = hashlib.sha1()
    for e in entries:
        digest.update(f"{e['path']}\0{e['size']}\0{e['modified']}\n".encode())
    etag = f'W/"{len(entries)}-{digest.hexdigest()}"'

    with _files_cache_lock:
        _files_cache.update(key=KB_DIR, at=now, etag=etag, entries=entries)
    return etag, entries


def _invalidate_files_cache() -> None:
    with _files_cache_lock:
        _files_cache.update(key=None, at=0.0, etag="", entries=[])


# ---------------------------------------------------------------------------
# AP-MT execution-history index — auth
# ---------------------------------------------------------------------------
# Shared-secret header, checked on every write to /api/kb/executions*. Any
# container on the podman network can reach this route directly (Tier-2 apps
# are not behind the workspace's own IdentityGuard the way Tier-1 routes
# are) — so an unconfigured secret means CLOSED (503), never open. Mirrors
# the agents-platform-runners app's own execute_secret gate.
def _check_exec_secret(request: Request) -> Response | None:
    configured = os.environ.get("KB_EXEC_SECRET", "").strip()
    if not configured:
        return JSONResponse(
            {"error": "KB_EXEC_SECRET is not configured on this Knowledge Base install"},
            status_code=503,
        )
    provided = request.headers.get("x-kb-exec-secret", "")
    if not provided or provided != configured:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


def _run_job(code: str) -> None:
    """Execute `code` in a fresh Python subprocess, streaming stdout/stderr."""
    global _job_state
    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "FASTEMBED_CACHE_PATH": os.environ.get("FASTEMBED_CACHE_PATH", "/tmp/fastembed_cache"),
    }
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=APP_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            line = line.rstrip("\n")
            with _job_lock:
                _job_state["output"].append(line)
                # Keep a reasonable cap so memory doesn't grow unboundedly
                if len(_job_state["output"]) > 500:
                    _job_state["output"] = _job_state["output"][-500:]
        proc.wait()
        with _job_lock:
            if proc.returncode != 0:
                _job_state["error"] = f"Process exited with code {proc.returncode}"
            else:
                _job_state["error"] = None
    except Exception as exc:
        with _job_lock:
            _job_state["error"] = str(exc)
    finally:
        with _job_lock:
            _job_state["running"] = False
            _job_state["last_run"] = datetime.utcnow().isoformat() + "Z"


def _start_job(operation: str, code: str) -> dict:
    """Start a background job if none is running. Returns status dict."""
    with _job_lock:
        if _job_state["running"]:
            return {"error": "A job is already running", "running": True}
        _job_state["running"] = True
        _job_state["operation"] = operation
        _job_state["output"] = []
        _job_state["error"] = None

    t = threading.Thread(target=_run_job, args=(code,), daemon=True)
    t.start()
    return {"started": True, "operation": operation}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class KnowledgeBaseRoutes:
    def __init__(self, router: APIRouter):
        # File CRUD
        router.get("/api/kb/files")(self.list_files)
        router.get("/api/kb/file/{path:path}")(self.read_file)
        router.put("/api/kb/file/{path:path}")(self.save_file)
        router.delete("/api/kb/file/{path:path}")(self.delete_file)
        router.get("/api/kb/search")(self.search_files)
        router.get("/api/kb/mcp-search")(self.mcp_search)
        # Build / map
        router.post("/api/kb/build")(self.build)
        router.post("/api/kb/map")(self.map_path)
        router.post("/api/kb/map-and-build")(self.map_and_build)
        router.get("/api/kb/status")(self.get_status)
        router.get("/api/kb/doc-count")(self.get_doc_count)
        # Settings (map_paths) — this app's own local replacement for the
        # monolith's shared /api/settings/aw round-trip.
        router.get("/api/kb/settings")(self.get_settings)
        router.put("/api/kb/settings")(self.save_settings)
        router.post("/api/kb/add-repo")(self.add_repo)
        router.get("/api/kb/repos")(self.list_repos)
        # AP-MT execution-history index — separate table, separate MCP tool,
        # see kb_app/exec_pg.py's module docstring for why.
        router.post("/api/kb/executions")(self.post_executions)
        router.post("/api/kb/executions/prune")(self.post_executions_prune)
        router.get("/api/kb/executions/status")(self.get_executions_status)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _safe_path(self, path: str) -> str | None:
        """Resolve path and ensure it's inside KB_DIR."""
        full = os.path.realpath(os.path.join(KB_DIR, path))
        if not full.startswith(os.path.realpath(KB_DIR)):
            return None
        return full

    # ------------------------------------------------------------------
    # File CRUD
    # ------------------------------------------------------------------

    async def list_files(self, request: Request, response: Response):
        """List all files in the knowledge base as a flat list.

        Conditional: send back the ETag from a previous response as
        ``If-None-Match`` and an unchanged tree answers 304 with no body.
        The 200 shape is unchanged — [{path, name, size, modified}].
        """
        if not os.path.isdir(KB_DIR):
            return []

        etag, entries = _files_snapshot()
        # no-store on the 304 path too: the browser must keep asking us, we
        # just want the answer to be cheap, not skipped.
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        response.headers.update(headers)
        return entries

    async def read_file(self, path: str):
        full = self._safe_path(path)
        if not full or not os.path.isfile(full):
            return {"error": "File not found", "success": False}
        with open(full) as f:
            return {"path": path, "content": f.read(), "success": True}

    async def save_file(self, path: str, data: dict = Body(...)):
        full = self._safe_path(path)
        if not full:
            return {"error": "Invalid path", "success": False}
        os.makedirs(os.path.dirname(full), exist_ok=True)
        content = data.get("content", "")
        with open(full, "w") as f:
            f.write(content)
        _invalidate_files_cache()

        def do_update():
            try:
                from . import kb_pg
                parts = path.split("/", 1)
                repo = parts[0] if len(parts) > 1 else "local"
                fpath = parts[1] if len(parts) > 1 else path
                kb_pg.upsert(
                    doc_id=path,
                    content=content,
                    metadata={"repo": repo, "path": fpath},
                )
            except Exception as e:
                log.warning(f"failed to index KB document {path}: {e}")

        threading.Thread(target=do_update, daemon=True).start()
        return {"success": True, "path": path}

    async def delete_file(self, path: str):
        full = self._safe_path(path)
        if not full or not os.path.isfile(full):
            return {"error": "File not found", "success": False}
        os.remove(full)
        parent = os.path.dirname(full)
        while parent != os.path.realpath(KB_DIR):
            if not os.listdir(parent):
                os.rmdir(parent)
                parent = os.path.dirname(parent)
            else:
                break
        _invalidate_files_cache()

        def do_delete():
            try:
                from . import kb_pg
                kb_pg.delete(path)
            except Exception as e:
                log.warning(f"failed to remove KB document {path} from index: {e}")

        threading.Thread(target=do_delete, daemon=True).start()
        return {"success": True}

    async def search_files(self, q: str = ""):
        """Search files by name or content (grep-style)."""
        if not q or not os.path.isdir(KB_DIR):
            return []
        q_lower = q.lower()
        results = []
        for root, dirs, files in os.walk(KB_DIR):
            for f in files:
                if f.startswith("."):
                    continue
                full_path = os.path.join(root, f)
                rel = os.path.relpath(full_path, KB_DIR)
                name_match = q_lower in f.lower() or q_lower in rel.lower()
                content_match = False
                snippet = ""
                try:
                    with open(full_path) as fh:
                        content = fh.read()
                    idx = content.lower().find(q_lower)
                    if idx >= 0:
                        content_match = True
                        start = max(0, idx - 50)
                        end = min(len(content), idx + len(q) + 100)
                        snippet = ("..." if start > 0 else "") + content[start:end] + ("..." if end < len(content) else "")
                except Exception:
                    pass  # unreadable file (binary/encoding/permissions) — skip from search results
                if name_match or content_match:
                    results.append({
                        "path": rel,
                        "name": f,
                        "name_match": name_match,
                        "content_match": content_match,
                        "snippet": snippet,
                    })
                if len(results) >= 50:
                    break
        return results

    async def mcp_search(self, q: str = "", n: int = 5):
        """Semantic search via pgvector."""
        if not q:
            return []
        try:
            from . import kb_pg
            total = kb_pg.count()
            if total == 0:
                return []
            results = kb_pg.search(q, n_results=min(n, total))
            items = []
            for r in results:
                items.append({
                    "id": r["id"],
                    "content": r["content"][:500],
                    "metadata": r["metadata"],
                    "score": round(max(0.0, float(r["score"])), 3),
                })
            return items
        except Exception as e:
            return {"error": str(e), "results": []}

    # ------------------------------------------------------------------
    # Settings (map_paths)
    # ------------------------------------------------------------------

    async def get_settings(self):
        return _settings.get_settings()

    async def save_settings(self, data: dict = Body(...)):
        return _settings.save_settings(data)

    # ------------------------------------------------------------------
    # Build / map endpoints
    # ------------------------------------------------------------------

    async def build(self, data: dict = Body(default={})):
        """Start a build job in the background."""
        force = data.get("force", False)
        args = ["--build"]
        if force:
            args.append("--force")
        args_repr = repr(args)
        code = (
            "import sys; sys.path.insert(0, '.'); "
            f"from kb_app.kb_ops import run; run({args_repr})"
        )
        return _start_job("build", code)

    async def map_path(self, data: dict = Body(...)):
        """Map a single path."""
        path = data.get("path", ".")
        force = data.get("force", False)
        args = ["--map-path", path]
        if force:
            args.append("--force")
        args_repr = repr(args)
        code = (
            "import sys; sys.path.insert(0, '.'); "
            f"from kb_app.kb_ops import run; run({args_repr})"
        )
        return _start_job(f"map:{path}", code)

    async def map_and_build(self, data: dict = Body(...)):
        """Map multiple paths sequentially then build."""
        paths = data.get("paths", ["."])
        force = data.get("force", False)

        # Build a single Python expression that chains all map calls then build
        calls = []
        for p in paths:
            args = ["--map-path", p]
            if force:
                args.append("--force")
            calls.append(f"run({repr(args)})")

        build_args = ["--build"]
        if force:
            build_args.append("--force")
        calls.append(f"run({repr(build_args)})")

        code = (
            "import sys; sys.path.insert(0, '.'); "
            "from kb_app.kb_ops import run; "
            + "; ".join(calls)
        )
        return _start_job("map-and-build", code)

    async def add_repo(self, data: dict = Body(...)):
        """Clone (or pull) a git repo into REPOS_DIR so it can be mapped by
        name — this container has no bind mount into any other repo's
        checkout (unlike a package-relative path, which only resolves
        something actually inside the container's own filesystem)."""
        git_url = (data.get("git_url") or "").strip()
        if not git_url:
            return {"error": "git_url is required"}
        name = (data.get("name") or "").strip() or None
        args = ["--add-repo", git_url]
        if name:
            args += ["--name", name]
        args_repr = repr(args)
        code = (
            "import sys; sys.path.insert(0, '.'); "
            f"from kb_app.kb_ops import run; run({args_repr})"
        )
        return _start_job(f"add-repo:{name or git_url}", code)

    async def list_repos(self):
        """Every bare name a Mapped Folders entry can actually resolve to —
        surfaced in the UI so typing a container filesystem path by mistake,
        e.g. /opt/aw-workspace/repos, is obviously wrong before you hit Map.

        ``folders`` is returned separately from the combined ``repos`` list
        (which still contains everything, for older UI builds): the two are
        different promises. A folder is something the user deliberately
        mapped at the workspace level and can be ANY directory, while a repo
        name is just whatever happens to be cloned under the workspace's
        repos/ dir — so the UI can say "you mapped these" instead of
        flattening both into one anonymous list of names.
        """
        from .kb_ops import _available_repo_names, _mapped_folder_names
        return {
            "repos": _available_repo_names(),
            "folders": _mapped_folder_names(),
        }

    async def get_status(self):
        """Return current job status."""
        with _job_lock:
            return dict(_job_state)

    async def get_doc_count(self):
        """Return total number of documents in pgvector."""
        try:
            from . import kb_pg
            return {"count": kb_pg.count()}
        except Exception as e:
            return {"count": 0, "error": str(e)}

    # ------------------------------------------------------------------
    # AP-MT execution-history index
    # ------------------------------------------------------------------

    async def post_executions(self, request: Request, data: dict = Body(...)):
        """Upsert one run's chunks. Body: {run_id, chunks: [{seq, content,
        metadata}], metadata_common}. Idempotent per run — reindexing the
        same run_id replaces its chunks, never duplicates them."""
        auth_err = _check_exec_secret(request)
        if auth_err:
            return auth_err

        run_id = (data.get("run_id") or "").strip()
        if not run_id:
            return JSONResponse({"error": "run_id is required"}, status_code=400)

        chunks_in = data.get("chunks")
        if not isinstance(chunks_in, list) or not chunks_in:
            return JSONResponse({"error": "chunks must be a non-empty list"}, status_code=400)

        metadata_common = data.get("metadata_common") or {}
        chunks = []
        for c in chunks_in:
            if not isinstance(c, dict) or c.get("seq") is None or not c.get("content"):
                return JSONResponse(
                    {"error": f"each chunk needs seq and content: {c!r}"}, status_code=400,
                )
            meta = dict(metadata_common)
            meta.update(c.get("metadata") or {})
            chunks.append({"seq": c["seq"], "content": c["content"], "metadata": meta})

        from . import exec_pg
        try:
            n = exec_pg.upsert_chunks(run_id, chunks)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            log.warning(f"failed to index execution run {run_id}: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        return {"success": True, "run_id": run_id, "chunks_indexed": n}

    async def post_executions_prune(self, request: Request, data: dict = Body(default={})):
        """Manual retention sweep — same logic the daily lifespan task runs."""
        auth_err = _check_exec_secret(request)
        if auth_err:
            return auth_err

        from . import exec_pg
        result = exec_pg.prune(
            retention_days=data.get("retention_days"),
            max_rows=data.get("max_rows"),
        )
        return {"success": True, **result}

    async def get_executions_status(self):
        """Observability: chunk/run counts, oldest/newest indexed_at. Read-only,
        no secret required — mirrors /api/kb/doc-count's own posture."""
        from . import exec_pg
        return exec_pg.status()


def build_routes() -> APIRouter:
    router = APIRouter()
    KnowledgeBaseRoutes(router)
    return router
