"""MCP server for the Knowledge Base — semantic search/update over the
pgvector-backed store, plus skill discovery.

Ported from agentic-workspace's src/mcp/knowledge_base.py, which was a
**stdio** MCP server (one child process per client, spawned by whatever
launched it). This app is a Tier-2 (container) aw-workspace app — the
aw-mcp-gateway that aggregates MCP tools runs in a SIBLING container and
cannot spawn a process inside this one, so the same tool surface is
re-exposed here over Streamable HTTP instead (POST /mcp, JSON-RPC 2.0 —
same wire protocol aw-mcp-gateway's own HttpUpstream speaks). See
``main.py``'s ``/mcp`` route and ``self_register.py`` for how this app
tells the gateway where to find it.

Tool handlers below are logic-for-logic the same as the original stdio
server, with two adaptations now that this runs as a long-lived server
process rather than a short-lived per-connection child:
* ``update_knowledge_base``/``delete_knowledge_base`` call ``kb_ops``
  in-process directly (the original shelled out to ``./aw knowledge-base``
  via subprocess — there is no such CLI wrapper binary here, and an
  in-process call is strictly cheaper for a server that stays up).
* No ``AW_BIN``/``AW_PYTHON`` — this app has no monolith ``./aw`` CLI.
"""

from __future__ import annotations

import os
import re

from . import kb_ops
from . import kb_pg as _kb

SKILLS_DIR = os.environ.get("KB_SKILLS_DIR", "/app/skills")


def _tool_result(text, is_error=False):
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


TOOLS_SCHEMA = [
    {
        "name": "search_knowledge_base",
        "description": (
            "Search the project knowledge base for relevant documentation. "
            "Returns matching documents from the indexed knowledge base repositories. "
            "Use this when you need information about platform architecture, APIs, "
            "configuration, deployment, or development workflows."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query",
                },
                "n_results": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "update_knowledge_base",
        "description": (
            "Create or update a knowledge base document. The file is written to "
            "the knowledge base's data directory with proper frontmatter (source, "
            "checksum, edited timestamp) and then imported into the pgvector index. "
            "Use this to add new documentation or correct existing entries."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path within the knowledge base. Use the correct "
                        "subfolder for the content type: 'memory/<topic>.md' for "
                        "agent-generated lessons, fixes, and debugging findings; "
                        "'docs/<area>/<topic>.md' for architecture/platform documentation."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "The markdown content for the document (without frontmatter)",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "delete_knowledge_base",
        "description": (
            "Delete a knowledge base document. Removes the file from the "
            "knowledge base's data directory and from the pgvector index."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the knowledge base, e.g. 'docs/platform/old-topic.md'",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_skills",
        "description": (
            "Search for AW skills by semantic similarity. Returns skills whose "
            "name/description match the query — use this to discover which skill "
            "to load for a given task before calling load_skill."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language query describing the task or skill you're looking for",
                },
                "n_results": {
                    "type": "integer",
                    "description": "Max number of skills to return (default: 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "load_skill",
        "description": (
            "Load the full content of a skill SKILL.md by skill name. Returns the "
            "complete skill instructions so you can follow them. Use search_skills "
            "first to discover the exact skill name."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill directory name (e.g. 'aw-kb-curator')",
                },
            },
            "required": ["name"],
        },
    },
]

_DISPATCH_NAMES = {t["name"] for t in TOOLS_SCHEMA}


def _search_knowledge_base(query: str, n_results: int = 5) -> dict:
    if not query:
        return _tool_result("Please provide a search query.", is_error=True)

    total = _kb.count()
    if total == 0:
        return _tool_result(
            "Knowledge base is empty or not built yet.\n"
            "Run a build via POST /api/kb/build (or the Manage panel in the UI).",
            is_error=True,
        )

    n_results = min(n_results, total)
    results = _kb.search(query, n_results=n_results)

    if not results:
        return _tool_result("No results found.", is_error=True)

    output_parts = [f"Found {len(results)} results for: {query}\n"]
    for i, r in enumerate(results):
        score = r["score"]
        meta = r["metadata"]
        source = meta.get("source", "")
        repo = meta.get("repo", "")
        path = meta.get("path", "")

        content = r["content"]
        if len(content) > 2000:
            content = content[:2000] + "\n\n... (truncated)"

        output_parts.append(
            f"--- Result {i+1} (score: {score:.3f}) ---\n"
            f"Source: {source}\n"
            f"Repo: {repo} | Path: {path}\n\n"
            f"{content}\n"
        )

    return _tool_result("\n".join(output_parts))


def _update_knowledge_base(path: str, content: str) -> dict:
    if not path or not content:
        return _tool_result("Both 'path' and 'content' are required.", is_error=True)
    try:
        kb_ops.update(path, content)
        return _tool_result(f"Updated: {path}")
    except Exception as e:
        return _tool_result(f"Error: {e}", is_error=True)


def _delete_knowledge_base(path: str) -> dict:
    if not path:
        return _tool_result("'path' is required.", is_error=True)
    try:
        existed = kb_ops.delete(path)
        return _tool_result(f"Deleted: {path}" if existed else f"Not found: {path}")
    except Exception as e:
        return _tool_result(f"Error: {e}", is_error=True)


def _search_skills(query: str, n_results: int = 5) -> dict:
    if not query:
        return _tool_result("Please provide a search query.", is_error=True)

    try:
        total = _kb.count()
        if total > 0:
            candidates = _kb.search(query, n_results=min(40, total))
            skill_results = [r for r in candidates if r["metadata"].get("repo") == "skills"][:n_results]
        else:
            skill_results = []
    except Exception:
        skill_results = []

    if not skill_results:
        return _search_skills_filesystem(query, n_results)

    lines = [f"Found {len(skill_results)} skills matching: {query}\n"]
    for i, r in enumerate(skill_results):
        meta = r["metadata"]
        path = meta.get("path", "")
        parts = path.split("/")
        skill_name = parts[1] if len(parts) >= 2 else path
        content = r["content"]
        if len(content) > 400:
            content = content[:400] + "…"
        lines.append(
            f"--- Skill {i+1} (score: {r['score']:.3f}) ---\n"
            f"name: {skill_name}\n"
            f"path: skills/{skill_name}/SKILL.md\n\n"
            f"{content}\n"
        )
    return _tool_result("\n".join(lines))


def _search_skills_filesystem(query: str, n_results: int) -> dict:
    """Fallback skill search scanning SKILL.md files from the filesystem."""
    if not os.path.isdir(SKILLS_DIR):
        return _tool_result("skills directory not found.", is_error=True)

    query_lower = query.lower()
    query_words = set(re.split(r"\W+", query_lower))

    matches = []
    for name in sorted(os.listdir(SKILLS_DIR)):
        skill_path = os.path.join(SKILLS_DIR, name, "SKILL.md")
        if not os.path.isfile(skill_path):
            continue
        try:
            with open(skill_path, encoding="utf-8") as f:
                content = f.read(800)
        except OSError:
            continue
        content_lower = content.lower()
        content_words = set(re.split(r"\W+", content_lower))
        score = len(query_words & content_words) / max(len(query_words), 1)
        if score > 0 or query_lower in content_lower or name.lower() in query_lower:
            matches.append((score, name, content))

    matches.sort(key=lambda x: -x[0])
    matches = matches[:n_results]

    if not matches:
        return _tool_result(f"No skills matched '{query}'.")

    lines = [f"Found {len(matches)} skills matching: {query}\n"]
    for score, name, content in matches:
        preview = content[:400] + ("…" if len(content) > 400 else "")
        lines.append(f"--- Skill ---\nname: {name}\npath: skills/{name}/SKILL.md\n\n{preview}\n")
    return _tool_result("\n".join(lines))


def _load_skill(name: str) -> dict:
    if not name:
        return _tool_result("'name' is required.", is_error=True)
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        return _tool_result(f"Invalid skill name: {name!r}", is_error=True)
    skill_path = os.path.join(SKILLS_DIR, name, "SKILL.md")
    if not os.path.isfile(skill_path):
        return _tool_result(
            f"Skill '{name}' not found at {skill_path}. Use search_skills to find the correct name.",
            is_error=True,
        )
    try:
        with open(skill_path, encoding="utf-8") as f:
            content = f.read()
        return _tool_result(f"# Skill: {name}\n# Path: {skill_path}\n\n{content}")
    except OSError as e:
        return _tool_result(f"Error reading skill: {e}", is_error=True)


_HANDLERS = {
    "search_knowledge_base": lambda a: _search_knowledge_base(a.get("query", ""), a.get("n_results", 5)),
    "update_knowledge_base": lambda a: _update_knowledge_base(a.get("path", ""), a.get("content", "")),
    "delete_knowledge_base": lambda a: _delete_knowledge_base(a.get("path", "")),
    "search_skills": lambda a: _search_skills(a.get("query", ""), a.get("n_results", 5)),
    "load_skill": lambda a: _load_skill(a.get("name", "")),
}


def handle_request(request: dict) -> dict | None:
    """Handle one JSON-RPC 2.0 message. Returns None for notifications."""
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "aw-app-kb", "version": "1.0.0"},
            },
        }

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_SCHEMA}}

    if method == "tools/call":
        params = request.get("params", {}) or {}
        tool_name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        handler = _HANDLERS.get(tool_name)
        if handler is None:
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": _tool_result(f"Unknown tool: {tool_name}", is_error=True)}
        try:
            result = handler(args)
        except Exception as exc:
            result = _tool_result(f"{tool_name} crashed: {exc}", is_error=True)
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}
