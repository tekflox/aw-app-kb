"""Knowledge Base file browser + editor API.

Ported from agentic-workspace's src/api/routes/knowledge_base.py. Uses
kb_pg (pgvector) for semantic operations. Background job runner for
build/map operations runs as a subprocess to avoid loading the 520 MB
fastembed model into the API process (same reasoning as the original).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from datetime import datetime

from fastapi import APIRouter, Body

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

    async def list_files(self):
        """List all files in the knowledge base as a flat list."""
        if not os.path.isdir(KB_DIR):
            return []
        result = []
        for root, dirs, files in os.walk(KB_DIR):
            dirs.sort()
            for f in sorted(files):
                if f.startswith("."):
                    continue
                full_path = os.path.join(root, f)
                rel = os.path.relpath(full_path, KB_DIR)
                stat = os.stat(full_path)
                result.append({
                    "path": rel,
                    "name": f,
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                })
        return result

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
        the shared, already-checked-out workspace repos/ dir (read-only
        $AW_WORKSPACE_REPOS mount) plus kb's own private clones — surfaced
        in the UI so typing a container filesystem path by mistake, e.g.
        /opt/aw-workspace/repos, is obviously wrong before you hit Map."""
        from .kb_ops import _available_repo_names
        return {"repos": _available_repo_names()}

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


def build_routes() -> APIRouter:
    router = APIRouter()
    KnowledgeBaseRoutes(router)
    return router
