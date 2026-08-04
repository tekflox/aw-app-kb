"""Tests for kb_app/kb_ops.py's self-map detection and git-based repo ingestion.

kb_ops imports kb_pg at module level, but kb_pg itself only imports fastembed/
psycopg lazily inside functions (see kb_pg.py) — so importing kb_ops here never
loads the 520 MB embedding model or needs a real Postgres.
"""
from __future__ import annotations

import subprocess

from kb_app import kb_ops


def test_resolve_map_target_bare_name_uses_repos_dir():
    repo_dir, repo_name, extra_skips = kb_ops._resolve_map_target("agentic-workspace")
    assert repo_dir == f"{kb_ops.REPOS_DIR}/agentic-workspace"
    assert repo_name == "agentic-workspace"
    assert extra_skips == set()


def test_resolve_map_target_self_map_skips_kb_output_and_repos():
    # BASE_DIR used to be an undefined name — this call would raise NameError.
    repo_dir, repo_name, extra_skips = kb_ops._resolve_map_target(kb_ops.BASE_DIR)
    assert repo_dir == kb_ops.BASE_DIR
    assert extra_skips == {"knowledge_base", "repos"}


def test_resolve_map_target_explicit_path_no_self_skip(tmp_path):
    repo_dir, repo_name, extra_skips = kb_ops._resolve_map_target(str(tmp_path))
    assert repo_dir == str(tmp_path)
    assert repo_name == tmp_path.name
    assert extra_skips == set()


def test_add_repo_clones_then_pulls(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_ops, "REPOS_DIR", str(tmp_path / "repos"))

    # A local bare-ish repo to clone from (no network needed).
    src = tmp_path / "src-repo"
    src.mkdir()
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "test"], check=True)
    (src / "README.md").write_text("hello")
    subprocess.run(["git", "-C", str(src), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "init"], check=True)

    name = kb_ops._add_repo(str(src))
    assert name == "src-repo"
    cloned = tmp_path / "repos" / "src-repo"
    assert (cloned / "README.md").read_text() == "hello"

    # Second call (repo already present) should pull instead of re-clone.
    (src / "README.md").write_text("updated")
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-am", "update"], check=True)
    kb_ops._add_repo(str(src), name="src-repo")
    assert (cloned / "README.md").read_text() == "updated"


def test_add_repo_custom_name(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_ops, "REPOS_DIR", str(tmp_path / "repos"))
    src = tmp_path / "another"
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "test"], check=True)
    (src / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "init"], check=True)

    name = kb_ops._add_repo(str(src), name="custom-name")
    assert name == "custom-name"
    assert (tmp_path / "repos" / "custom-name" / "f.txt").exists()
