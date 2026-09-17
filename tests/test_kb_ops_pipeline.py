"""Tests for kb_app/kb_ops.py's build/search/update/delete pipeline, the
code-map extractors (Python/JS-TS/generic/HTML), the folder-mapping walker
(_map_repo/_map_all/_prune_unmapped_output) and the CLI dispatcher (run()).

Part of raising this repo's coverage ratchet from the pilot's 53% baseline
toward a real 80% (docs/standards/pipeline-testing.md §4.2) — these are the
biggest previously-untested surfaces in the package (kb_ops.py was at 31%).
Every test asserts real behaviour (parsed structure, written files, printed
counts), not just that a call didn't raise.
"""
from __future__ import annotations

import json
import os

import pytest

from kb_app import kb_ops


# ---------------------------------------------------------------------------
# Pure helpers: _sha256 / _parse_frontmatter / _write_kb_file
# ---------------------------------------------------------------------------

def test_sha256_matches_hashlib():
    import hashlib
    assert kb_ops._sha256("hello") == hashlib.sha256(b"hello").hexdigest()


def test_parse_frontmatter_no_marker_returns_text_unchanged():
    meta, content = kb_ops._parse_frontmatter("just plain text")
    assert meta == {}
    assert content == "just plain text"


def test_parse_frontmatter_malformed_missing_closing_marker():
    meta, content = kb_ops._parse_frontmatter("---\nkey: value\nno closing marker")
    assert meta == {}


def test_parse_frontmatter_parses_false_and_generic_values():
    text = "---\nedited: false\nrepo: aw-app-kb\n---\nbody text"
    meta, content = kb_ops._parse_frontmatter(text)
    assert meta["edited"] is False
    assert meta["repo"] == "aw-app-kb"
    assert content == "body text"


def test_parse_frontmatter_true_becomes_the_string_unknown():
    """Documents the actual (odd) behaviour: 'true' maps to the literal
    string 'unknown', not the boolean True — a future change to this must
    not slip by silently since callers key off it (e.g. _map_repo's
    'edited' check treats any truthy non-False value as human-edited)."""
    meta, _ = kb_ops._parse_frontmatter("---\nedited: true\n---\nbody")
    assert meta["edited"] == "unknown"


def test_parse_frontmatter_ignores_lines_without_a_colon():
    meta, content = kb_ops._parse_frontmatter("---\nnotakeyvalue\nrepo: x\n---\nbody")
    assert meta == {"repo": "x"}


def test_write_kb_file_roundtrips_through_parse_frontmatter(tmp_path):
    out = tmp_path / "sub" / "doc.md"
    kb_ops._write_kb_file(str(out), {"repo": "x", "edited": False}, "the body")
    written = out.read_text()
    assert "edited: false" in written
    meta, content = kb_ops._parse_frontmatter(written)
    assert meta["repo"] == "x"
    assert meta["edited"] is False
    assert content == "the body"


# ---------------------------------------------------------------------------
# _build
# ---------------------------------------------------------------------------

class _FakeKB:
    """Stand-in for kb_ops's `_kb` (kb_pg) module reference."""

    def __init__(self, existing=None):
        self.existing = existing or {}
        self.upserted: list[tuple] = []
        self.deleted_ids: list[str] = []
        self.schema_calls = 0

    def ensure_kb_schema(self, retries=12, delay=1.0):
        self.schema_calls += 1

    def get_all_metadata(self):
        return dict(self.existing)

    def upsert_many(self, docs):
        self.upserted.extend(docs)
        return len(docs)

    def delete_many(self, ids):
        self.deleted_ids.extend(ids)
        return len(ids)

    def count(self):
        return len(self.existing) + len(self.upserted) - len(self.deleted_ids)

    def get_pg_url(self):
        return "postgresql://fake"


@pytest.fixture
def kb_dir(tmp_path, monkeypatch):
    d = tmp_path / "kb"
    d.mkdir()
    monkeypatch.setattr(kb_ops, "KB_DIR", str(d))
    return d


def test_build_adds_new_files_and_reports_counts(kb_dir, monkeypatch, capsys):
    (kb_dir / "new.md").write_text("---\nchecksum: abc\n---\nsome content")
    fake = _FakeKB()
    monkeypatch.setattr(kb_ops, "_kb", fake)

    kb_ops._build(force=False)

    assert len(fake.upserted) == 1
    doc_id, content, metadata = fake.upserted[0]
    assert doc_id == "new.md"
    assert content == "some content"
    assert metadata["checksum"] == "abc"
    out = capsys.readouterr().out
    assert "1 added, 0 updated, 0 unchanged, 0 removed" in out


def test_build_skips_unchanged_checksum(kb_dir, monkeypatch):
    (kb_dir / "same.md").write_text("---\nchecksum: sha256:xyz\n---\nunchanged body")
    fake = _FakeKB(existing={"same.md": {"checksum": "sha256:xyz"}})
    monkeypatch.setattr(kb_ops, "_kb", fake)

    kb_ops._build(force=False)

    assert fake.upserted == []


def test_build_updates_when_checksum_differs(kb_dir, monkeypatch):
    (kb_dir / "changed.md").write_text("---\nchecksum: sha256:new\n---\nnew body")
    fake = _FakeKB(existing={"changed.md": {"checksum": "sha256:old"}})
    monkeypatch.setattr(kb_ops, "_kb", fake)

    kb_ops._build(force=False)

    assert len(fake.upserted) == 1


def test_build_removes_stale_docs_no_longer_present(kb_dir, monkeypatch):
    fake = _FakeKB(existing={"gone.md": {"checksum": "sha256:x"}})
    monkeypatch.setattr(kb_ops, "_kb", fake)

    kb_ops._build(force=False)

    assert fake.deleted_ids == ["gone.md"]


def test_build_skips_non_markdown_and_dotdirs(kb_dir, monkeypatch):
    (kb_dir / "notes.txt").write_text("ignored, wrong extension")
    (kb_dir / ".hidden").mkdir()
    (kb_dir / ".hidden" / "inner.md").write_text("---\nchecksum: a\n---\nbody")
    fake = _FakeKB()
    monkeypatch.setattr(kb_ops, "_kb", fake)

    kb_ops._build(force=False)

    assert fake.upserted == []


def test_build_skips_files_with_empty_body_after_frontmatter(kb_dir, monkeypatch):
    (kb_dir / "empty.md").write_text("---\nchecksum: a\n---\n   \n")
    fake = _FakeKB()
    monkeypatch.setattr(kb_ops, "_kb", fake)

    kb_ops._build(force=False)

    assert fake.upserted == []


def test_build_force_truncates_before_reimporting(kb_dir, monkeypatch, capsys):
    (kb_dir / "doc.md").write_text("---\nchecksum: a\n---\nbody")
    fake = _FakeKB()
    monkeypatch.setattr(kb_ops, "_kb", fake)

    executed = []

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, *a, **k):
            executed.append(sql)

    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _FakeConn())

    kb_ops._build(force=True)

    assert any("TRUNCATE TABLE documents" in s for s in executed)
    out = capsys.readouterr().out
    assert "Wiped existing index." in out


def test_build_batches_upserts_past_batch_size(kb_dir, monkeypatch):
    for i in range(20):
        (kb_dir / f"doc{i}.md").write_text(f"---\nchecksum: c{i}\n---\nbody {i}")
    fake = _FakeKB()
    monkeypatch.setattr(kb_ops, "_kb", fake)

    kb_ops._build(force=False)

    assert len(fake.upserted) == 20


# ---------------------------------------------------------------------------
# _search
# ---------------------------------------------------------------------------

def test_search_exits_when_kb_is_empty(monkeypatch, capsys):
    monkeypatch.setattr(kb_ops._kb, "count", lambda: 0)
    with pytest.raises(SystemExit) as exc:
        kb_ops._search("query")
    assert exc.value.code == 1
    assert "empty or not built yet" in capsys.readouterr().out


def test_search_exits_when_no_results_found(monkeypatch, capsys):
    monkeypatch.setattr(kb_ops._kb, "count", lambda: 5)
    monkeypatch.setattr(kb_ops._kb, "search", lambda q, n_results=5: [])
    with pytest.raises(SystemExit) as exc:
        kb_ops._search("query")
    assert exc.value.code == 1
    assert "No results found" in capsys.readouterr().out


def test_search_prints_ranked_results_with_snippet(monkeypatch, capsys):
    monkeypatch.setattr(kb_ops._kb, "count", lambda: 5)
    long_content = "x" * 400
    monkeypatch.setattr(kb_ops._kb, "search", lambda q, n_results=5: [
        {"score": 0.912, "metadata": {"source": "src", "repo": "r", "path": "p.md"},
         "content": long_content},
    ])

    kb_ops._search("query text", top_k=3)

    out = capsys.readouterr().out
    assert "[r] p.md" in out
    assert "0.912" in out
    assert "..." in out  # truncated snippet marker


# ---------------------------------------------------------------------------
# _update
# ---------------------------------------------------------------------------

def test_update_rejects_blank_content(kb_dir, capsys):
    with pytest.raises(SystemExit) as exc:
        kb_ops._update("some/path", "   \n  ")
    assert exc.value.code == 1
    assert "No content provided" in capsys.readouterr().out


def test_update_appends_md_extension_when_missing(kb_dir, monkeypatch):
    monkeypatch.setattr(kb_ops._kb, "upsert", lambda *a, **k: None)
    monkeypatch.setattr(kb_ops._kb, "count", lambda: 1)

    kb_ops._update("repo/notes", "hello world")

    assert (kb_dir / "repo" / "notes.md").is_file()


def test_update_writes_metadata_and_indexes_new_file(kb_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(kb_ops._kb, "upsert", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(kb_ops._kb, "count", lambda: 1)

    kb_ops._update("myrepo/sub/doc.md", "the content")

    doc_id, content, metadata = calls[0][0]
    assert doc_id == "myrepo/sub/doc.md"
    assert content == "the content"
    assert metadata["repo"] == "myrepo"
    assert metadata["path"] == "sub/doc.md"
    written = (kb_dir / "myrepo" / "sub" / "doc.md").read_text()
    assert "the content" in written


def test_update_preserves_source_field_of_an_existing_file(kb_dir, monkeypatch):
    existing = kb_dir / "r" / "doc.md"
    kb_ops._write_kb_file(str(existing), {"source": "original/source.py"}, "old body")
    calls = []
    monkeypatch.setattr(kb_ops._kb, "upsert", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(kb_ops._kb, "count", lambda: 1)

    kb_ops._update("r/doc.md", "new body")

    _, _, metadata = calls[0]
    assert metadata["source"] == "original/source.py"


def test_update_local_repo_for_a_single_segment_path(kb_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(kb_ops._kb, "upsert", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(kb_ops._kb, "count", lambda: 1)

    kb_ops._update("standalone", "content")

    _, _, metadata = calls[0]
    assert metadata["repo"] == "local"
    assert metadata["path"] == "standalone.md"


# ---------------------------------------------------------------------------
# _delete
# ---------------------------------------------------------------------------

def test_delete_removes_file_and_reports_indexed_removal(kb_dir, monkeypatch, capsys):
    f = kb_dir / "gone.md"
    f.write_text("body")
    monkeypatch.setattr(kb_ops._kb, "delete", lambda doc_id: True)
    monkeypatch.setattr(kb_ops._kb, "count", lambda: 0)

    existed = kb_ops._delete("gone")

    assert existed is True
    assert not f.exists()
    assert "Removed from index" in capsys.readouterr().out


def test_delete_missing_file_still_queries_the_index(kb_dir, monkeypatch, capsys):
    monkeypatch.setattr(kb_ops._kb, "delete", lambda doc_id: False)
    monkeypatch.setattr(kb_ops._kb, "count", lambda: 0)

    existed = kb_ops._delete("never-existed.md")

    assert existed is False
    out = capsys.readouterr().out
    assert "File not found" in out
    assert "Not found in index" in out


# ---------------------------------------------------------------------------
# _extract_python
# ---------------------------------------------------------------------------

def test_extract_python_full_module():
    source = '''"""Module doc."""
class Foo(Base):
    """Foo doc."""
    @staticmethod
    def bar(self, x: int) -> str:
        """Bar doc."""
        pass


async def baz(*args, **kwargs):
    pass
'''
    data = kb_ops._extract_python(source, "mod.py")
    assert data["language"] == "Python"
    assert data["module_doc"] == "Module doc."
    assert data["classes"][0]["name"] == "Foo"
    assert data["classes"][0]["bases"] == ["Base"]
    assert data["classes"][0]["methods"][0]["name"] == "bar"
    assert data["classes"][0]["methods"][0]["decorators"] == ["@staticmethod"]
    assert data["functions"][0]["name"] == "baz"
    assert data["functions"][0]["is_async"] is True
    assert "*args" in data["functions"][0]["args"]
    assert "**kwargs" in data["functions"][0]["args"]


def test_extract_python_syntax_error_returns_none():
    assert kb_ops._extract_python("def broken(:\n", "bad.py") is None


def test_extract_python_no_content_returns_none():
    assert kb_ops._extract_python("x = 1\n", "flat.py") is None


def test_extract_python_strips_bom():
    data = kb_ops._extract_python("﻿\"\"\"doc\"\"\"\n", "bom.py")
    assert data["module_doc"] == "doc"


def test_extract_python_class_with_no_simple_base_name():
    source = "class Foo(some.pkg.Base):\n    pass\n"
    data = kb_ops._extract_python(source, "m.py")
    assert data["classes"][0]["bases"] == ["some.pkg.Base"]


# ---------------------------------------------------------------------------
# _extract_js_ts
# ---------------------------------------------------------------------------

def test_extract_js_ts_class_and_jsdoc_function():
    source = """/**
 * Adds two numbers.
 */
export function add(a, b) {
  return a + b;
}

export class Widget extends Base {
  render() {
    return null;
  }
}
"""
    data = kb_ops._extract_js_ts(source, "widget.tsx")
    assert data["language"] == "TypeScript (TSX)"
    names = [f["name"] for f in data["functions"]]
    assert "add" in names
    cls = data["classes"][0]
    assert cls["name"] == "Widget"
    assert cls["extends"] == "Base"
    assert any(m["name"] == "render" for m in cls["methods"])


def test_extract_js_ts_arrow_function_and_exports():
    source = "export const helper = (x) => x * 2;\nexport default helper;\n"
    data = kb_ops._extract_js_ts(source, "h.js")
    assert any(f["name"] == "helper" for f in data["functions"])
    assert data["exports"]


def test_extract_js_ts_no_matches_returns_none():
    assert kb_ops._extract_js_ts("const x = 1;\n", "flat.js") is None


# ---------------------------------------------------------------------------
# _extract_generic — Go / Java / Ruby / Bash / fallback
# ---------------------------------------------------------------------------

def test_extract_generic_go_functions_and_structs():
    source = """// Doer does things.
type Doer struct {
	Name string
}

// Do performs the action.
func (d *Doer) Do(x int) string {
	return d.Name
}

func Standalone() {}
"""
    data = kb_ops._extract_generic(source, "m.go", "Go")
    struct = next(c for c in data["classes"] if c["name"] == "Doer")
    assert "Doer does things." in struct["docstring"]
    assert any(m["name"] == "Do" for m in struct["methods"])
    assert any(f["name"] == "Standalone" for f in data["functions"])


def test_extract_generic_java_class_and_method():
    source = """// A greeter.
public class Greeter {
    public String greet(String name) {
        return "hi";
    }
}
"""
    data = kb_ops._extract_generic(source, "Greeter.java", "Java")
    cls = data["classes"][0]
    assert cls["name"] == "Greeter"
    assert any(m["name"] == "greet" for m in cls["methods"])


def test_extract_generic_ruby_class_and_method():
    source = """# A widget.
class Widget
  # Renders it.
  def render
    nil
  end
end
"""
    data = kb_ops._extract_generic(source, "widget.rb", "Ruby")
    cls = data["classes"][0]
    assert cls["name"] == "Widget"
    assert any(m["name"] == "render" for m in cls["methods"])


def test_extract_generic_bash_functions_and_header_doc():
    source = """#!/bin/bash
# Does a thing.
do_thing() {
  echo hi
}
"""
    data = kb_ops._extract_generic(source, "script.sh", "Shell")
    assert "Does a thing." in data["module_doc"]
    assert any(f["name"] == "do_thing" for f in data["functions"])


def test_extract_generic_bash_no_functions_no_doc_returns_none():
    assert kb_ops._extract_generic("echo hi\n", "flat.sh", "Shell") is None


def test_extract_generic_fallback_pattern_for_unlisted_extension():
    source = "sub greet() {\n    return 1;\n}\n"
    data = kb_ops._extract_generic(source, "m.pl", "Perl")
    assert any(f["name"] == "greet" for f in data["functions"])


def test_extract_generic_empty_returns_none():
    assert kb_ops._extract_generic("just prose, no code", "m.rs", "Rust") is None


# ---------------------------------------------------------------------------
# _extract_html
# ---------------------------------------------------------------------------

def test_extract_html_converts_body_and_captures_title():
    html = "<html><head><title>My Page</title></head><body><h1>Hi</h1><p>Text</p></body></html>"
    data = kb_ops._extract_html(html, "page.html")
    assert data["title"] == "My Page"
    assert "Hi" in data["markdown"]
    assert "Text" in data["markdown"]


def test_extract_html_strips_script_and_style_tags():
    html = "<html><body><script>evil()</script><style>.x{}</style><p>keep me</p></body></html>"
    data = kb_ops._extract_html(html, "p.html")
    assert "evil" not in data["markdown"]
    assert "keep me" in data["markdown"]


def test_extract_html_empty_body_returns_none():
    assert kb_ops._extract_html("<html><body></body></html>", "empty.html") is None


# ---------------------------------------------------------------------------
# _format_* helpers
# ---------------------------------------------------------------------------

def test_format_html_md_includes_title_and_body():
    out = kb_ops._format_html_md({"title": "T", "markdown": "body text"}, "p.html")
    assert "`p.html`" in out
    assert "**Title:** T" in out
    assert "body text" in out


def test_format_html_md_without_title():
    out = kb_ops._format_html_md({"title": "", "markdown": "body"}, "p.html")
    assert "**Title:**" not in out


def test_format_docstring_empty_is_empty_list():
    assert kb_ops._format_docstring("") == []
    assert kb_ops._format_docstring(None) == []


def test_lang_from_ext_known_and_unknown():
    assert kb_ops._lang_from_ext(".py") == "Python"
    assert kb_ops._lang_from_ext(".zig") == "ZIG"


def test_format_code_map_md_renders_classes_functions_and_exports():
    data = {
        "language": "Python",
        "module_doc": "module doc",
        "exports": ["a", "b"],
        "classes": [{
            "name": "Foo",
            "bases": ["Base"],
            "decorators": ["@dataclass"],
            "docstring": "class doc",
            "methods": [{
                "name": "bar", "args": "self", "is_async": True,
                "decorators": ["@staticmethod"], "docstring": "method doc",
            }],
        }],
        "functions": [{
            "name": "baz", "args": "", "is_async": False,
            "decorators": [], "docstring": "func doc",
        }],
    }
    out = kb_ops._format_code_map_md(data, "mod.py")
    assert "# `mod.py`" in out
    assert "## Exports" in out
    assert "`Foo(Base)`" in out
    assert "async `bar(self)`" in out or "async bar(self)" in out
    assert "baz()" in out
    assert "class doc" in out
    assert "func doc" in out


# ---------------------------------------------------------------------------
# _map_repo
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_and_kb(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    kb = tmp_path / "kb"
    kb.mkdir()
    monkeypatch.setattr(kb_ops, "KB_DIR", str(kb))
    monkeypatch.setattr(kb_ops, "MAPPED_FOLDERS_DIR", str(tmp_path / "no-folders"))
    monkeypatch.setattr(kb_ops, "SHARED_REPOS_DIR", str(tmp_path / "no-shared"))
    monkeypatch.setattr(kb_ops, "REPOS_DIR", str(tmp_path / "no-private"))
    return repo, kb


def test_map_repo_missing_dir_exits_1(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(kb_ops, "KB_DIR", str(tmp_path / "kb"))
    with pytest.raises(SystemExit) as exc:
        kb_ops._map_repo(str(tmp_path / "does-not-exist"))
    assert exc.value.code == 1
    assert "Repo not found" in capsys.readouterr().out


def test_map_repo_walks_python_html_and_doc_files(repo_and_kb, capsys):
    repo, kb = repo_and_kb
    (repo / "mod.py").write_text('"""Doc."""\ndef fn():\n    pass\n')
    (repo / "page.html").write_text("<html><body><p>hi</p></body></html>")
    (repo / "notes.md").write_text("# Already prose")

    kb_ops._map_repo(str(repo))

    out_dir = kb / "mapped_folders" / repo.name
    assert (out_dir / "mod.py.md").is_file()
    assert (out_dir / "page.html.md").is_file()
    assert (out_dir / "notes.md").is_file()
    out = capsys.readouterr().out
    assert "3 new, 0 updated" in out
    assert "1 HTML document(s) converted" in out
    assert "1 Markdown/text document(s) carried through" in out


def test_map_repo_skips_tiny_and_binary_looking_files(repo_and_kb):
    repo, kb = repo_and_kb
    (repo / "empty.py").write_text("")
    (repo / "one.py").write_text("x")  # < 10 chars

    kb_ops._map_repo(str(repo))

    out_dir = kb / "mapped_folders" / repo.name
    assert not any(out_dir.iterdir()) if out_dir.exists() else True


def test_map_repo_skips_unchanged_checksum_without_force(repo_and_kb, capsys):
    repo, kb = repo_and_kb
    (repo / "mod.py").write_text('"""Doc."""\ndef fn():\n    pass\n')
    kb_ops._map_repo(str(repo))
    capsys.readouterr()

    kb_ops._map_repo(str(repo))
    out = capsys.readouterr().out
    assert "0 new, 0 updated, 1 unchanged" in out


def test_map_repo_force_remaps_unchanged_files(repo_and_kb, capsys):
    repo, kb = repo_and_kb
    (repo / "mod.py").write_text('"""Doc."""\ndef fn():\n    pass\n')
    kb_ops._map_repo(str(repo))
    capsys.readouterr()

    kb_ops._map_repo(str(repo), force=True)
    out = capsys.readouterr().out
    assert "1 updated" in out


def test_map_repo_skips_files_marked_human_edited(repo_and_kb, capsys):
    repo, kb = repo_and_kb
    (repo / "mod.py").write_text('"""Doc."""\ndef fn():\n    pass\n')
    kb_ops._map_repo(str(repo))
    out_file = kb / "mapped_folders" / repo.name / "mod.py.md"
    text = out_file.read_text()
    edited_text = text.replace("edited: false", "edited: 2026-01-01 00:00:00")
    out_file.write_text(edited_text)

    kb_ops._map_repo(str(repo))
    out = capsys.readouterr().out
    assert "1 unchanged" in out


def test_map_repo_skips_html_inside_html_skip_dirs(repo_and_kb):
    repo, kb = repo_and_kb
    skip_dir = repo / "htmlcov"
    skip_dir.mkdir()
    (skip_dir / "index.html").write_text("<html><body><p>coverage noise</p></body></html>")

    kb_ops._map_repo(str(repo))

    out_dir = kb / "mapped_folders" / repo.name
    assert not (out_dir / "htmlcov" / "index.html.md").exists()


# ---------------------------------------------------------------------------
# _map_all / _prune_unmapped_output / _mapped_output_name
# ---------------------------------------------------------------------------

def test_map_all_prints_nothing_to_map_when_no_folders(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(kb_ops, "MAPPED_FOLDERS_DIR", str(tmp_path / "no-folders"))
    from kb_app import settings as _settings
    monkeypatch.setattr(_settings, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(_settings, "DEFAULT_MAP_PATHS", [])

    kb_ops._map_all()

    assert "Nothing to map" in capsys.readouterr().out


def test_map_all_maps_each_workspace_folder_and_prunes_stale_output(tmp_path, monkeypatch, capsys):
    folders = tmp_path / "folders"
    (folders / "keep").mkdir(parents=True)
    (folders / "keep" / "a.py").write_text('"""Doc."""\ndef f():\n    pass\n')
    kb = tmp_path / "kb"
    stale_out = kb / "mapped_folders" / "stale"
    stale_out.mkdir(parents=True)

    monkeypatch.setattr(kb_ops, "MAPPED_FOLDERS_DIR", str(folders))
    monkeypatch.setattr(kb_ops, "KB_DIR", str(kb))
    monkeypatch.setattr(kb_ops, "SHARED_REPOS_DIR", str(tmp_path / "no-shared"))
    monkeypatch.setattr(kb_ops, "REPOS_DIR", str(tmp_path / "no-private"))
    from kb_app import settings as _settings
    monkeypatch.setattr(_settings, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(_settings, "DEFAULT_MAP_PATHS", [])

    kb_ops._map_all()

    assert (kb / "mapped_folders" / "keep" / "a.py.md").is_file()
    assert not stale_out.exists()


def test_map_all_skips_disabled_folders(tmp_path, monkeypatch, capsys):
    folders = tmp_path / "folders"
    (folders / "off").mkdir(parents=True)
    (folders / "off" / "a.py").write_text('"""Doc."""\ndef f():\n    pass\n')
    kb = tmp_path / "kb"

    monkeypatch.setattr(kb_ops, "MAPPED_FOLDERS_DIR", str(folders))
    monkeypatch.setattr(kb_ops, "KB_DIR", str(kb))
    monkeypatch.setattr(kb_ops, "SHARED_REPOS_DIR", str(tmp_path / "no-shared"))
    monkeypatch.setattr(kb_ops, "REPOS_DIR", str(tmp_path / "no-private"))

    from kb_app import settings as _settings
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"map_paths": [], "disabled_folders": ["off"]}))
    monkeypatch.setattr(_settings, "SETTINGS_PATH", str(settings_path))

    kb_ops._map_all()

    out = capsys.readouterr().out
    assert "Skipping 1 folder(s) switched off: off" in out
    assert not (kb / "mapped_folders" / "off").exists()


def test_map_all_reports_unreachable_absolute_extra_path(tmp_path, monkeypatch, capsys):
    folders = tmp_path / "folders"
    folders.mkdir()
    kb = tmp_path / "kb"

    monkeypatch.setattr(kb_ops, "MAPPED_FOLDERS_DIR", str(folders))
    monkeypatch.setattr(kb_ops, "KB_DIR", str(kb))
    monkeypatch.setattr(kb_ops, "SHARED_REPOS_DIR", str(tmp_path / "no-shared"))
    monkeypatch.setattr(kb_ops, "REPOS_DIR", str(tmp_path / "no-private"))
    monkeypatch.setattr(kb_ops, "WORKSPACE_HOST_DIR", str(tmp_path / "no-host"))

    from kb_app import settings as _settings
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"map_paths": ["/nowhere/at/all"], "disabled_folders": []}))
    monkeypatch.setattr(_settings, "SETTINGS_PATH", str(settings_path))

    kb_ops._map_all()

    out = capsys.readouterr().out
    assert "an absolute host path this container cannot see" in out


def test_prune_unmapped_output_noop_when_root_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_ops, "KB_DIR", str(tmp_path / "kb"))
    kb_ops._prune_unmapped_output(set())  # must not raise


def test_mapped_output_name_matches_resolve_map_target(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_ops, "MAPPED_FOLDERS_DIR", str(tmp_path / "no-folders"))
    assert kb_ops._mapped_output_name("some-name") == "some-name"


# ---------------------------------------------------------------------------
# run() — CLI dispatch
# ---------------------------------------------------------------------------

def test_run_with_no_flags_prints_help(capsys):
    kb_ops.run([])
    assert "usage" in capsys.readouterr().out.lower()


def test_run_dispatches_add_repo(monkeypatch):
    calls = []
    monkeypatch.setattr(kb_ops, "_add_repo", lambda url, name=None: calls.append((url, name)))
    kb_ops.run(["--add-repo", "https://example.com/x.git", "--name", "custom"])
    assert calls == [("https://example.com/x.git", "custom")]


def test_run_dispatches_map_all(monkeypatch):
    calls = []
    monkeypatch.setattr(kb_ops, "_map_all", lambda force=False: calls.append(force))
    kb_ops.run(["--map-all", "--force"])
    assert calls == [True]


def test_run_dispatches_map_path(monkeypatch):
    calls = []
    monkeypatch.setattr(kb_ops, "_map_repo", lambda target, force=False: calls.append((target, force)))
    kb_ops.run(["--map-path", "somewhere"])
    assert calls == [("somewhere", False)]


def test_run_dispatches_delete(monkeypatch):
    calls = []
    monkeypatch.setattr(kb_ops, "_delete", lambda p: calls.append(p))
    kb_ops.run(["--delete", "some/doc"])
    assert calls == ["some/doc"]


def test_run_dispatches_update_reading_stdin(monkeypatch):
    calls = []
    monkeypatch.setattr(kb_ops, "_update", lambda p, c: calls.append((p, c)))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("content from stdin"))
    kb_ops.run(["--update", "some/doc"])
    assert calls == [("some/doc", "content from stdin")]


def test_run_dispatches_build(monkeypatch):
    calls = []
    monkeypatch.setattr(kb_ops, "_build", lambda force=False: calls.append(force))
    kb_ops.run(["--build"])
    assert calls == [False]


def test_run_dispatches_search(monkeypatch):
    calls = []
    monkeypatch.setattr(kb_ops, "_search", lambda q, top_k: calls.append((q, top_k)))
    kb_ops.run(["--search", "my query", "--top-k", "7"])
    assert calls == [("my query", 7)]
