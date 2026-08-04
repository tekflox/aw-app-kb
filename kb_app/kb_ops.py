"""Manage the knowledge base — build vector index, search, update.

Ported from agentic-workspace's src/commands/knowledge_base.py (the
``./aw knowledge-base`` CLI). Kept as a plain CLI-callable module (rather
than folded into routes.py) because routes.py's build/map endpoints spawn
it as a background subprocess (see routes.py's ``_run_job``) — the
520 MB fastembed model must NOT be loaded into the main FastAPI event
loop's process.

Usage:
    python -m kb_app.kb_ops --build             # import changed docs into pgvector
    python -m kb_app.kb_ops --search "how to configure redir"
    python -m kb_app.kb_ops --update <path>     # create/update a KB file (content from stdin)
    python -m kb_app.kb_ops --map-path <path>   # extract code map (classes/functions/docstrings) to MD
                                                # also converts .html/.htm to MD via markdownify
                                                # writes to <KB_DIR>/mapped_folders/<name>/
    python -m kb_app.kb_ops --map-path . --build   # map this workspace then index it

Storage backend: PostgreSQL + pgvector, bundled into this app's own container.
"""

import argparse
import ast
import hashlib
import os
import re
import sys
from datetime import datetime

COMMAND = "knowledge-base"
DESCRIPTION = "Manage the knowledge base (build, search, map)"

from . import kb_pg as _kb

# Persistent volume (see aw-app.json's "data" volume -> /app/data) — survives
# container recreation, unlike the rest of the image.
DATA_DIR = os.environ.get("KB_DATA_DIR", "/app/data")
# KB_DIR (the mapped/indexed markdown tree) is normally DATA_DIR/knowledge_base,
# but can be pinned to its own mount (see aw-app.json's $AW_KB_DIR volume,
# which makes this survive under the workspace's own .aw-workspace/knowledge_base
# instead of being nested inside this app's private data dir).
KB_DIR = os.environ.get("KB_DIR_OVERRIDE") or os.path.join(DATA_DIR, "knowledge_base")
REPOS_DIR = os.path.join(DATA_DIR, "repos")
# The "self" directory for --map-path self-mapping detection (skip our own
# output/data dirs rather than looping into them) — DATA_DIR is the modern
# analog of the monolith's repo root, since KB_DIR and REPOS_DIR both live
# under it.
BASE_DIR = DATA_DIR


def _sha256(content):
    return hashlib.sha256(content.encode()).hexdigest()


def _parse_frontmatter(text):
    """Parse YAML-like frontmatter between --- markers.

    edited: false means not edited (auto-generated from source).
    edited: 2026-03-29 12:34:56 means human-edited at that time — skip re-import from source.
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip()
            if v.lower() == "false":
                v = False
            elif v.lower() == "true":
                v = "unknown"
            meta[k.strip()] = v
    return meta, parts[2].strip()


def _write_kb_file(path, meta, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = ["---"]
    for k, v in meta.items():
        if v is False:
            lines.append(f"{k}: false")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(content)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def _build(force=False):
    """Import changed docs from KB_DIR into the pgvector index."""
    print("Building pgvector index (aw-pgvector @ 127.0.0.1:5433)...")

    # Bootstrap schema (idempotent, retries until aw-pgvector is ready).
    _kb.ensure_kb_schema(retries=12, delay=1.0)

    if force:
        # Wipe the table and re-import everything.
        import psycopg
        with psycopg.connect(_kb.get_pg_url(), autocommit=True) as conn:
            conn.execute("TRUNCATE TABLE documents")
        print("  Wiped existing index.")

    existing = _kb.get_all_metadata()  # {doc_id: metadata_dict}

    added = 0
    updated = 0
    unchanged = 0
    removed = 0
    seen_ids: set[str] = set()
    batch: list[tuple[str, str, dict]] = []
    BATCH_SIZE = 16  # embed + upsert in chunks to limit memory use

    def _flush_batch():
        nonlocal added, updated
        if not batch:
            return
        _kb.upsert_many(batch)
        batch.clear()

    for root, dirs, files in os.walk(KB_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath) as f:
                    text = f.read()
            except Exception:
                continue

            meta, content = _parse_frontmatter(text)
            if not content.strip():
                continue

            rel_path = os.path.relpath(fpath, KB_DIR)
            doc_id = rel_path

            repo = meta.get("repo", rel_path.split("/")[0])
            path = meta.get("path", rel_path)
            checksum = meta.get("checksum", "")
            seen_ids.add(doc_id)

            ex = existing.get(doc_id)
            if ex and ex.get("checksum") == checksum:
                unchanged += 1
                continue

            metadata = {
                "source": meta.get("source", ""),
                "repo": repo,
                "path": path,
                "checksum": checksum,
            }
            batch.append((doc_id, content, metadata))

            if doc_id in existing:
                updated += 1
            else:
                added += 1

            if len(batch) >= BATCH_SIZE:
                _flush_batch()

    _flush_batch()

    stale = set(existing.keys()) - seen_ids
    if stale:
        removed = _kb.delete_many(list(stale))

    total = _kb.count()
    print(f"\nBuild complete: {added} added, {updated} updated, {unchanged} unchanged, {removed} removed")
    print(f"Total documents in index: {total}")



def _search(query, top_k=5):
    total = _kb.count()
    if total == 0:
        print("Knowledge base is empty or not built yet.")
        print("Run: ./aw knowledge-base --build")
        print("(Requires aw-pgvector container to be running.)")
        sys.exit(1)

    results = _kb.search(query, n_results=top_k)
    if not results:
        print("No results found (aw-pgvector may be offline).")
        sys.exit(1)

    print(f"\nSearch results for: {query}\n")
    for i, r in enumerate(results):
        score = r["score"]
        meta = r["metadata"]
        source = meta.get("source", "?")
        repo = meta.get("repo", "?")
        path = meta.get("path", "?")
        doc = r["content"]
        snippet = doc[:300].replace("\n", " ").strip()
        if len(doc) > 300:
            snippet += "..."
        print(f"  {i+1}. [{repo}] {path}  (score: {score:.3f})")
        print(f"     {source}")
        print(f"     {snippet}")
        print()



def _update(rel_path, content):
    """Create or update a knowledge base file and import it into pgvector."""
    if not content.strip():
        print("ERROR: No content provided (reads from stdin).")
        sys.exit(1)

    if not rel_path.endswith(".md"):
        rel_path += ".md"

    out_path = os.path.join(KB_DIR, rel_path)

    parts = rel_path.split("/", 1)
    repo = parts[0] if len(parts) > 1 else "local"
    path = parts[1] if len(parts) > 1 else rel_path

    checksum = _sha256(content)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    source = ""
    if os.path.isfile(out_path):
        with open(out_path) as f:
            old_meta, _ = _parse_frontmatter(f.read())
        source = old_meta.get("source", "")

    meta = {
        "source": source,
        "repo": repo,
        "path": path,
        "checksum": f"sha256:{checksum}",
        "edited": now,
    }

    _write_kb_file(out_path, meta, content)
    print(f"Written: {out_path}")

    doc_id = rel_path
    _kb.upsert(doc_id, content, {
        "source": source,
        "repo": repo,
        "path": path,
        "checksum": f"sha256:{checksum}",
    })
    total = _kb.count()
    print(f"Indexed: {doc_id} (collection: {total} docs)")



def _delete(rel_path):
    """Delete a knowledge base file and remove it from pgvector."""
    if not rel_path.endswith(".md"):
        rel_path += ".md"

    file_path = os.path.join(KB_DIR, rel_path)

    if os.path.isfile(file_path):
        os.unlink(file_path)
        print(f"Deleted: {file_path}")
    else:
        print(f"File not found: {file_path}")

    existed = _kb.delete(rel_path)
    total = _kb.count()
    if existed:
        print(f"Removed from index: {rel_path} (collection: {total} docs)")
    else:
        print(f"Not found in index: {rel_path}")
    return existed


# Public library aliases — mcp_http.py calls these directly (in-process,
# this is a long-lived server, not a per-invocation CLI process). The
# leading-underscore names above stay as-is since run()/the CLI dispatcher
# below already call them that way.
build = _build
search = _search
update = _update
delete = _delete


CODE_EXTENSIONS = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript (JSX)",
    ".ts": "TypeScript",
    ".tsx": "TypeScript (TSX)",
    ".java": "Java",
    ".go": "Go",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".c": "C",
    ".cpp": "C++",
    ".h": "C/C++ Header",
    ".hpp": "C++ Header",
    ".cs": "C#",
    ".swift": "Swift",
    ".kt": "Kotlin",
    ".scala": "Scala",
    ".php": "PHP",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".lua": "Lua",
    ".r": "R",
    ".R": "R",
    ".pl": "Perl",
    ".pm": "Perl",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hs": "Haskell",
    ".clj": "Clojure",
    ".vue": "Vue",
    ".svelte": "Svelte",
}

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".tox", ".venv", "venv", ".env",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".next", ".nuxt",
    "coverage", ".coverage", "egg-info", ".eggs", "vendor", "bower_components",
    "target", ".gradle", "bin", "obj",
}

# HTML files are treated as documents (converted to Markdown), not code.
# Tracked separately so we can list them in --help and skip generated noise.
HTML_EXTENSIONS = {".html": "HTML", ".htm": "HTML"}

# Extra directories to skip when walking for HTML — coverage reports,
# recorded sessions, test fixtures, and other generated HTML noise that
# would flood the KB without adding signal.
HTML_SKIP_DIRS = {
    "coverage_html", "htmlcov", "coverage-report", "coverage_report",
    ".tmp", "tmp", "recordings", "site-packages", "_site", ".cache",
    "test-results", "playwright-report", "allure-report",
}


def _extract_python(source, rel_path):
    """Extract classes, functions, and docstrings from Python using AST."""
    if source.startswith("\ufeff"):
        source = source[1:]
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    module_doc = ast.get_docstring(tree)
    classes = []
    functions = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            cls_info = {
                "name": node.name,
                "docstring": ast.get_docstring(node) or "",
                "bases": [],
                "decorators": [],
                "methods": [],
                "line": node.lineno,
            }
            for base in node.bases:
                try:
                    cls_info["bases"].append(ast.unparse(base))
                except (AttributeError, Exception):
                    if isinstance(base, ast.Name):
                        cls_info["bases"].append(base.id)
                    else:
                        cls_info["bases"].append("...")
            for dec in node.decorator_list:
                try:
                    cls_info["decorators"].append(f"@{ast.unparse(dec)}")
                except (AttributeError, Exception):
                    if isinstance(dec, ast.Name):
                        cls_info["decorators"].append(f"@{dec.id}")
                    else:
                        cls_info["decorators"].append("@...")

            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = _extract_py_args(item)
                    method_info = {
                        "name": item.name,
                        "args": args,
                        "docstring": ast.get_docstring(item) or "",
                        "decorators": [],
                        "line": item.lineno,
                        "is_async": isinstance(item, ast.AsyncFunctionDef),
                    }
                    for dec in item.decorator_list:
                        try:
                            method_info["decorators"].append(f"@{ast.unparse(dec)}")
                        except (AttributeError, Exception):
                            if isinstance(dec, ast.Name):
                                method_info["decorators"].append(f"@{dec.id}")
                            else:
                                method_info["decorators"].append("@...")
                    cls_info["methods"].append(method_info)

            classes.append(cls_info)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = _extract_py_args(node)
            func_info = {
                "name": node.name,
                "args": args,
                "docstring": ast.get_docstring(node) or "",
                "decorators": [],
                "line": node.lineno,
                "is_async": isinstance(node, ast.AsyncFunctionDef),
            }
            for dec in node.decorator_list:
                try:
                    func_info["decorators"].append(f"@{ast.unparse(dec)}")
                except (AttributeError, Exception):
                    if isinstance(dec, ast.Name):
                        func_info["decorators"].append(f"@{dec.id}")
                    else:
                        func_info["decorators"].append("@...")
            functions.append(func_info)

    if not module_doc and not classes and not functions:
        return None

    return {
        "language": "Python",
        "module_doc": module_doc,
        "classes": classes,
        "functions": functions,
    }


def _unparse_annotation(node):
    """Convert an AST annotation node to a readable string."""
    try:
        return ast.unparse(node)
    except AttributeError:
        pass
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Constant):
        return repr(node.value)
    elif isinstance(node, ast.Attribute):
        try:
            return ast.unparse(node)
        except Exception:
            return f"{_unparse_annotation(node.value)}.{node.attr}"
    elif isinstance(node, ast.Subscript):
        try:
            return ast.unparse(node)
        except Exception:
            return "..."
    return "..."


def _extract_py_args(node):
    """Extract function argument names from a FunctionDef node."""
    args = []
    for arg in node.args.args:
        name = arg.arg
        if arg.annotation:
            ann = _unparse_annotation(arg.annotation)
            args.append(f"{name}: {ann}")
        else:
            args.append(name)
    if node.args.vararg:
        args.append(f"*{node.args.vararg.arg}")
    if node.args.kwarg:
        args.append(f"**{node.args.kwarg.arg}")
    return ", ".join(args)


def _extract_js_ts(source, rel_path):
    """Extract classes, functions, and JSDoc from JS/TS/JSX/TSX using regex."""
    classes = []
    functions = []
    exports = []

    lines = source.split("\n")

    jsdoc_map = {}
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("/**"):
            doc_lines = []
            while i < len(lines):
                doc_lines.append(lines[i])
                if "*/" in lines[i]:
                    break
                i += 1
            doc_end = i
            doc_text = "\n".join(doc_lines)
            doc_text = re.sub(r"/\*\*\s*", "", doc_text)
            doc_text = re.sub(r"\s*\*/", "", doc_text)
            doc_text = re.sub(r"^\s*\*\s?", "", doc_text, flags=re.MULTILINE)
            doc_text = doc_text.strip()
            jsdoc_map[doc_end + 1] = doc_text
        i += 1

    for m in re.finditer(
        r"^(?:export\s+)?(?:default\s+)?class\s+(\w+)(?:\s+extends\s+([\w.]+))?",
        source,
        re.MULTILINE,
    ):
        line_num = source[:m.start()].count("\n") + 1
        cls = {
            "name": m.group(1),
            "extends": m.group(2) or "",
            "docstring": jsdoc_map.get(line_num, ""),
            "line": line_num,
            "methods": [],
        }
        classes.append(cls)

    func_patterns = [
        r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)",
        r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(?([^)]*?)\)?\s*=>",
        r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:React\.)?(?:memo|forwardRef|lazy)\(",
    ]

    for pattern in func_patterns:
        for m in re.finditer(pattern, source, re.MULTILINE):
            line_num = source[:m.start()].count("\n") + 1
            name = m.group(1)
            args = m.group(2).strip() if m.lastindex >= 2 else ""
            func = {
                "name": name,
                "args": args,
                "docstring": jsdoc_map.get(line_num, ""),
                "line": line_num,
                "is_async": "async" in m.group(0),
            }
            if not any(c["name"] == name for c in classes):
                functions.append(func)

    for m in re.finditer(
        r"^\s+(?:async\s+)?(\w+)\s*\(([^)]*)\)\s*\{",
        source,
        re.MULTILINE,
    ):
        line_num = source[:m.start()].count("\n") + 1
        name = m.group(1)
        if name in ("if", "for", "while", "switch", "catch", "constructor"):
            if name != "constructor":
                continue
        for cls in classes:
            if cls["line"] < line_num:
                cls["methods"].append({
                    "name": name,
                    "args": m.group(2).strip(),
                    "docstring": jsdoc_map.get(line_num, ""),
                    "line": line_num,
                })

    for m in re.finditer(r"^export\s+(?:default\s+)?(?:{\s*(.+?)\s*}|(\w+))", source, re.MULTILINE):
        exp = m.group(1) or m.group(2)
        if exp:
            exports.append(exp.strip())

    if not classes and not functions:
        return None

    return {
        "language": _lang_from_ext(os.path.splitext(rel_path)[1]),
        "module_doc": jsdoc_map.get(1, ""),
        "classes": classes,
        "functions": functions,
        "exports": exports[:20],
    }


def _extract_generic(source, rel_path, language):
    """Generic extraction for Go, Java, Ruby, etc. — regex-based, best-effort."""
    functions = []
    classes = []

    ext = os.path.splitext(rel_path)[1]

    def _find_preceding_comment(pos):
        """Look for a comment block immediately before `pos`."""
        before = source[:pos].rstrip()
        doc_lines = []

        lines_before = before.split("\n")
        for line in reversed(lines_before):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("#"):
                doc_lines.insert(0, re.sub(r"^[/#]+\s?", "", stripped))
            elif stripped.startswith("*") or stripped.startswith("/*"):
                doc_lines.insert(0, re.sub(r"^[/*]+\s?", "", stripped))
            elif stripped.endswith("*/"):
                doc_lines.insert(0, re.sub(r"\s*\*/$", "", stripped))
            elif not stripped:
                if doc_lines:
                    break
            else:
                break
        return "\n".join(doc_lines).strip()

    if ext == ".go":
        for m in re.finditer(r"^func\s+(?:\(\w+\s+\*?(\w+)\)\s+)?(\w+)\s*\(([^)]*)\)", source, re.MULTILINE):
            line_num = source[:m.start()].count("\n") + 1
            receiver = m.group(1)
            name = m.group(2)
            args = m.group(3).strip()
            doc = _find_preceding_comment(m.start())
            entry = {"name": name, "args": args, "docstring": doc, "line": line_num}
            if receiver:
                cls = next((c for c in classes if c["name"] == receiver), None)
                if not cls:
                    cls = {"name": receiver, "docstring": "", "line": 0, "methods": []}
                    classes.append(cls)
                cls["methods"].append(entry)
            else:
                functions.append(entry)

        for m in re.finditer(r"^type\s+(\w+)\s+struct\s*\{", source, re.MULTILINE):
            name = m.group(1)
            line_num = source[:m.start()].count("\n") + 1
            doc = _find_preceding_comment(m.start())
            cls = next((c for c in classes if c["name"] == name), None)
            if cls:
                cls["docstring"] = doc
                cls["line"] = line_num
            else:
                classes.append({"name": name, "docstring": doc, "line": line_num, "methods": []})

    elif ext in (".java", ".cs", ".kt", ".scala"):
        for m in re.finditer(
            r"^(?:public|private|protected|internal)?\s*(?:static\s+)?(?:abstract\s+)?(?:class|interface|enum)\s+(\w+)",
            source, re.MULTILINE
        ):
            line_num = source[:m.start()].count("\n") + 1
            doc = _find_preceding_comment(m.start())
            classes.append({"name": m.group(1), "docstring": doc, "line": line_num, "methods": []})

        for m in re.finditer(
            r"^\s+(?:public|private|protected)?\s*(?:static\s+)?(?:async\s+)?(?:\w+(?:<[^>]+>)?)\s+(\w+)\s*\(([^)]*)\)",
            source, re.MULTILINE
        ):
            line_num = source[:m.start()].count("\n") + 1
            name = m.group(1)
            if name in ("if", "for", "while", "switch", "catch", "return", "new", "throw"):
                continue
            doc = _find_preceding_comment(m.start())
            entry = {"name": name, "args": m.group(2).strip(), "docstring": doc, "line": line_num}
            assigned = False
            for cls in reversed(classes):
                if cls["line"] < line_num:
                    cls["methods"].append(entry)
                    assigned = True
                    break
            if not assigned:
                functions.append(entry)

    elif ext in (".rb",):
        for m in re.finditer(r"^(?:class|module)\s+(\w+(?:::\w+)*)", source, re.MULTILINE):
            line_num = source[:m.start()].count("\n") + 1
            doc = _find_preceding_comment(m.start())
            classes.append({"name": m.group(1), "docstring": doc, "line": line_num, "methods": []})

        for m in re.finditer(r"^\s*def\s+(\w+[\?!=]?)\s*(?:\(([^)]*)\))?", source, re.MULTILINE):
            line_num = source[:m.start()].count("\n") + 1
            name = m.group(1)
            args = m.group(2) or ""
            doc = _find_preceding_comment(m.start())
            entry = {"name": name, "args": args.strip(), "docstring": doc, "line": line_num}
            assigned = False
            for cls in reversed(classes):
                if cls["line"] < line_num:
                    cls["methods"].append(entry)
                    assigned = True
                    break
            if not assigned:
                functions.append(entry)

    elif ext in (".sh", ".bash", ".zsh"):
        # Bash: match both `function name { ... }` and `name() { ... }`.
        seen = set()
        patterns = [
            r"^\s*function\s+([A-Za-z_][\w-]*)\s*(?:\(\s*\))?\s*\{",
            r"^\s*([A-Za-z_][\w-]*)\s*\(\s*\)\s*\{",
        ]
        for pattern in patterns:
            for m in re.finditer(pattern, source, re.MULTILINE):
                name = m.group(1)
                if name in seen:
                    continue
                seen.add(name)
                line_num = source[:m.start()].count("\n") + 1
                doc = _find_preceding_comment(m.start())
                functions.append({
                    "name": name,
                    "args": "",
                    "docstring": doc,
                    "line": line_num,
                })

        # Capture the leading shebang + comment header as module-level doc
        # so scripts with no functions still produce useful KB entries.
        module_doc = ""
        header_lines = []
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#!"):
                header_lines.append(stripped[2:].strip())
                continue
            if stripped.startswith("#"):
                header_lines.append(re.sub(r"^#+\s?", "", stripped))
                continue
            if not stripped:
                if header_lines:
                    break
                continue
            break
        if header_lines:
            module_doc = "\n".join(header_lines).strip()

        if not functions and not module_doc:
            return None

        return {
            "language": language,
            "module_doc": module_doc,
            "classes": classes,
            "functions": functions,
        }

    else:
        for m in re.finditer(r"^(?:(?:pub|fn|def|func|function|sub)\s+)(\w+)\s*\(([^)]*)\)", source, re.MULTILINE):
            line_num = source[:m.start()].count("\n") + 1
            doc = _find_preceding_comment(m.start())
            functions.append({
                "name": m.group(1),
                "args": m.group(2).strip(),
                "docstring": doc,
                "line": line_num,
            })

    if not classes and not functions:
        return None

    return {
        "language": language,
        "module_doc": "",
        "classes": classes,
        "functions": functions,
    }


def _extract_html(source, rel_path):
    """Convert an HTML document to Markdown for KB indexing.

    Uses markdownify with conservative options so the result is plain prose
    plus headings/lists/links/code blocks — no raw HTML, no inline styles.
    Returns None for documents that contain no extractable text (empty body,
    JS-only pages, etc.) so the walker skips them.
    """
    try:
        from bs4 import BeautifulSoup
        from markdownify import markdownify
    except ImportError as exc:
        print(f"  WARN: HTML conversion unavailable ({exc}); run ./aw to bootstrap deps.")
        return None

    try:
        soup = BeautifulSoup(source, "html.parser")
    except Exception:
        return None

    # Drop chrome that pollutes the KB and produces no useful prose.
    for tag in soup(["script", "style", "noscript", "template", "svg", "presentation", "iframe"]):
        tag.decompose()

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    body = soup.body or soup
    html_body = str(body)

    try:
        md = markdownify(
            html_body,
            heading_style="ATX",
            bullets="-",
            strip=["meta", "link"],
            escape_asterisks=False,
            escape_underscores=False,
        )
    except Exception:
        return None

    # Collapse runs of blank lines that markdownify tends to leave behind.
    md = re.sub(r"\n{3,}", "\n\n", md).strip()

    if not md:
        return None

    return {
        "language": "HTML",
        "title": title,
        "markdown": md,
    }


def _format_html_md(data, rel_path):
    """Format an HTML extraction into a KB-ready Markdown document."""
    lines = [f"# `{rel_path}`", ""]
    lines.append("**Language:** HTML (converted)")
    lines.append("")
    if data.get("title"):
        lines.append(f"**Title:** {data['title']}")
        lines.append("")
    lines.append(data["markdown"])
    return "\n".join(lines)


def _lang_from_ext(ext):
    return CODE_EXTENSIONS.get(ext, ext.lstrip(".").upper())


def _format_docstring(doc, indent=""):
    """Format a full docstring as a blockquote block."""
    if not doc:
        return []
    lines = []
    for docline in doc.split("\n"):
        lines.append(f"{indent}> {docline}")
    lines.append("")
    return lines


def _format_code_map_md(data, rel_path):
    """Format extracted code data into markdown."""
    lines = []
    lang = data.get("language", "Unknown")
    lines.append(f"# `{rel_path}`\n")
    lines.append(f"**Language:** {lang}\n")

    module_doc = data.get("module_doc")
    if module_doc:
        lines.extend(_format_docstring(module_doc))

    exports = data.get("exports", [])
    if exports:
        lines.append("## Exports\n")
        lines.append(f"`{', '.join(exports)}`\n")

    classes = data.get("classes", [])
    if classes:
        lines.append("## Classes\n")
        for cls in classes:
            decorators = cls.get("decorators", [])
            for dec in decorators:
                lines.append(f"`{dec}`")
            bases = cls.get("bases", [])
            extends = cls.get("extends", "")
            inheritance = ""
            if bases:
                inheritance = f"({', '.join(bases)})"
            elif extends:
                inheritance = f" extends {extends}"
            lines.append(f"### `{cls['name']}{inheritance}`\n")

            if cls.get("docstring"):
                lines.extend(_format_docstring(cls["docstring"]))

            methods = cls.get("methods", [])
            if methods:
                lines.append("#### Methods\n")
                for meth in methods:
                    name = meth["name"]
                    args = meth.get("args", "")
                    is_async = meth.get("is_async", False)
                    prefix = "async " if is_async else ""
                    decorators_str = " ".join(meth.get("decorators", []))
                    if decorators_str:
                        decorators_str = f" {decorators_str}"
                    lines.append(f"##### `{prefix}{name}({args})`{decorators_str}\n")
                    doc = meth.get("docstring", "")
                    if doc:
                        lines.extend(_format_docstring(doc))

    functions = data.get("functions", [])
    if functions:
        lines.append("## Functions\n")
        for func in functions:
            name = func["name"]
            args = func.get("args", "")
            is_async = func.get("is_async", False)
            prefix = "async " if is_async else ""
            decorators = func.get("decorators", [])
            dec_str = " ".join(decorators)
            if dec_str:
                dec_str = f" {dec_str}"
            lines.append(f"### `{prefix}{name}({args})`{dec_str}\n")
            doc = func.get("docstring", "")
            if doc:
                lines.extend(_format_docstring(doc))

    return "\n".join(lines)


def _resolve_map_target(target):
    """Resolve a --map-path argument to (repo_dir, repo_name, extra_skips).

    Accepts:
      - "." or any path  -> that directory, name = basename of resolved path
      - a bare name      -> repos/<name> (for manually placed directories)

    When the resolved directory is BASE_DIR (the agentic-workspace root),
    extra_skips prevents the walker from looping into KB output or runtime data.
    """
    looks_like_path = (
        target == "."
        or target.startswith("/")
        or target.startswith("./")
        or target.startswith("../")
        or os.sep in target
    )

    if looks_like_path:
        repo_dir = os.path.abspath(target)
        repo_name = os.path.basename(repo_dir) or "root"
    else:
        repo_dir = os.path.join(REPOS_DIR, target)
        repo_name = target

    extra_skips = set()
    if os.path.abspath(repo_dir) == os.path.abspath(BASE_DIR):
        # Self-mapping: skip dirs that would loop or produce massive noise.
        # - knowledge_base/ : KB output — mapping it would index our own output
        # - repos/          : cloned/manually placed repos tracked separately
        # .venv/ and venv/ are already in SKIP_DIRS (applied to all walks).
        extra_skips = {"knowledge_base", "repos"}

    return repo_dir, repo_name, extra_skips


def _map_repo(target, force=False):
    """Walk a repo, extract code structure, write one MD per source file."""
    repo_dir, repo_name, extra_skips = _resolve_map_target(target)

    if not os.path.isdir(repo_dir):
        print(f"ERROR: Repo not found at {repo_dir}")
        if os.path.isdir(REPOS_DIR):
            print(f"Available repos: {', '.join(sorted(os.listdir(REPOS_DIR)))}")
        sys.exit(1)

    out_dir = os.path.join(KB_DIR, "mapped_folders", repo_name)
    os.makedirs(out_dir, exist_ok=True)

    # Code walks skip the standard noise; HTML walks skip the same set
    # plus generated-HTML dirs (coverage reports, recordings, etc.) that
    # would otherwise flood the KB with low-signal content.
    skip_dirs = SKIP_DIRS | extra_skips
    html_skip_dirs = skip_dirs | HTML_SKIP_DIRS

    total_new = 0
    total_updated = 0
    total_skipped = 0
    total_empty = 0
    total_html = 0

    print(f"Mapping {repo_dir} -> {out_dir}")
    if extra_skips:
        print(f"  Self-map skips: {', '.join(sorted(extra_skips))}")

    for root, dirs, files in os.walk(repo_dir):
        # Compute effective skip set per-directory: HTML files require the
        # stricter set, so we keep dirs that EITHER walk could traverse and
        # filter per-file below.
        dirs[:] = [
            d for d in dirs
            if d not in skip_dirs and not d.startswith(".")
        ]

        # Path components from repo root, used to gate HTML files out of
        # generated dirs without blocking code files in the same tree.
        rel_root = os.path.relpath(root, repo_dir)
        rel_parts = set() if rel_root == "." else set(rel_root.split(os.sep))
        in_html_skip = bool(rel_parts & HTML_SKIP_DIRS)

        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            is_code = ext in CODE_EXTENSIONS
            is_html = ext in HTML_EXTENSIONS and not in_html_skip
            if not (is_code or is_html):
                continue

            src_path = os.path.join(root, fname)
            rel_path = os.path.relpath(src_path, repo_dir)

            try:
                with open(src_path, encoding="utf-8", errors="replace") as f:
                    source = f.read()
            except Exception:
                continue

            if not source.strip() or len(source) < 10:
                total_empty += 1
                continue

            checksum = _sha256(source)

            out_rel = rel_path + ".md"
            out_path = os.path.join(out_dir, out_rel)

            if os.path.isfile(out_path) and not force:
                try:
                    with open(out_path) as f:
                        existing = f.read()
                    meta, _ = _parse_frontmatter(existing)
                    if meta.get("edited") and meta.get("edited") is not False:
                        total_skipped += 1
                        continue
                    if meta.get("checksum") == f"sha256:{checksum}":
                        total_skipped += 1
                        continue
                except Exception:
                    pass

            if is_html:
                data = _extract_html(source, rel_path)
                doc_type = "html-doc"
            else:
                language = CODE_EXTENSIONS[ext]
                if ext == ".py":
                    data = _extract_python(source, rel_path)
                elif ext in (".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"):
                    data = _extract_js_ts(source, rel_path)
                else:
                    data = _extract_generic(source, rel_path, language)
                doc_type = "code-map"

            if data is None:
                total_empty += 1
                continue

            if is_html:
                content = _format_html_md(data, rel_path)
                total_html += 1
            else:
                content = _format_code_map_md(data, rel_path)

            if os.path.isfile(out_path):
                total_updated += 1
            else:
                total_new += 1

            meta = {
                "source": f"mapped_folders/{repo_name}/{rel_path}",
                "repo": repo_name,
                "path": rel_path,
                "type": doc_type,
                "checksum": f"sha256:{checksum}",
                "edited": False,
            }
            _write_kb_file(out_path, meta, content)

    print(f"\nCode map for {repo_name}:")
    print(f"  {total_new} new, {total_updated} updated, {total_skipped} unchanged, {total_empty} skipped (empty/no structure)")
    if total_html:
        print(f"  {total_html} HTML document(s) converted to Markdown")
    print(f"  Output: {out_dir}")



def _build_parser():
    parser = argparse.ArgumentParser(
        prog="./aw knowledge-base",
        description="Manage the knowledge base — build vector index, search, map code.",
    )
    parser.add_argument("--build", action="store_true", help="Import changed docs into pgvector (aw-pgvector, port 5433)")
    parser.add_argument("--force", action="store_true", help="--build: wipe DB and reimport everything. --map-path: remap unchanged files")
    parser.add_argument("--search", metavar="QUERY", help="Search the knowledge base")
    parser.add_argument("--update", metavar="PATH", help="Create/update a KB file (content from stdin)")
    parser.add_argument("--delete", metavar="PATH", help="Delete a KB file and remove from pgvector")
    parser.add_argument("--map-path", metavar="PATH", help="Extract code map from a directory and convert .html/.htm to Markdown. Accepts '.' or any absolute/relative path. Output goes to <KB_DIR>/mapped_folders/<name>/.")
    parser.add_argument("--map-all", action="store_true", help="Map every path listed in knowledge_base.map_paths in aw.json.")
    parser.add_argument("--add-repo", metavar="GIT_URL", help="Clone (or pull) a git repo into REPOS_DIR so --map-path <name> can reach it — this container has no bind mount into other repos' checkouts.")
    parser.add_argument("--name", metavar="NAME", help="--add-repo: local name to clone under (default: repo basename)")
    parser.add_argument("--top-k", type=int, default=5, help="Number of search results (default: 5)")
    return parser


def _add_repo(git_url: str, name: str | None = None) -> str:
    """Clone (or pull, if already present) a git repo into REPOS_DIR/<name>.

    This is how kb gets visibility into source it doesn't otherwise have a
    filesystem path to — it runs in its own isolated Tier-2 container, with
    no bind mount into e.g. the agentic-workspace monolith's checkout, so
    ``--map-path <name>`` (a bare name, per ``_resolve_map_target``) needs
    something under REPOS_DIR to point at. name defaults to the repo's own
    basename (``foo/bar.git`` -> ``bar``).
    """
    import subprocess

    if not name:
        name = os.path.basename(git_url.rstrip("/"))
        if name.endswith(".git"):
            name = name[:-4]

    os.makedirs(REPOS_DIR, exist_ok=True)
    repo_dir = os.path.join(REPOS_DIR, name)

    # Private repos (e.g. the agentic-workspace monolith) need a token —
    # stored in this app's own settings.json (PUT /api/kb/settings), same
    # unencrypted-on-the-private-data-volume model settings.py already uses
    # for map_paths. Only injected for github.com https URLs.
    from .settings import get_settings
    token = get_settings().get("github_token")
    clone_url = git_url
    if token and clone_url.startswith("https://github.com/"):
        clone_url = clone_url.replace(
            "https://github.com/", f"https://x-access-token:{token}@github.com/"
        )

    if os.path.isdir(os.path.join(repo_dir, ".git")):
        print(f"Updating {name} ({repo_dir})...")
        subprocess.run(["git", "-C", repo_dir, "pull", "--ff-only"], check=True)
    else:
        print(f"Cloning {git_url} -> {repo_dir}...")
        subprocess.run(["git", "clone", "--depth", "1", clone_url, repo_dir], check=True)

    return name


def _map_all(force: bool = False) -> None:
    """Map every path listed in this app's own settings.json (map_paths)."""
    from .settings import get_settings
    paths = get_settings().get("map_paths", [])
    if not paths:
        print("No map_paths configured — set them via PUT /api/kb/settings.")
        return
    print(f"Mapping {len(paths)} path(s) from config: {', '.join(paths)}")
    for p in paths:
        _map_repo(p, force=force)


def run(args=None):
    parser = _build_parser()
    parsed = parser.parse_args(args)

    if not any([parsed.build, parsed.search, parsed.update, parsed.delete,
                parsed.map_path, parsed.map_all, parsed.add_repo]):
        parser.print_help()
        return

    if parsed.add_repo:
        _add_repo(parsed.add_repo, name=parsed.name)
        return

    if parsed.map_all:
        _map_all(force=parsed.force)
        return

    if parsed.map_path:
        _map_repo(parsed.map_path, force=parsed.force)
        return

    if parsed.delete:
        _delete(parsed.delete)
        return

    if parsed.update:
        content = sys.stdin.read()
        _update(parsed.update, content)
        return

    if parsed.build:
        _build(force=parsed.force)

    if parsed.search:
        _search(parsed.search, parsed.top_k)


if __name__ == "__main__":
    run()
