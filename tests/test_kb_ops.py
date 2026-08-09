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


def test_resolve_map_target_prefers_shared_repos_dir_over_private_clone(tmp_path, monkeypatch):
    shared = tmp_path / "shared-repos"
    private = tmp_path / "private-repos"
    (shared / "agentic-workspace").mkdir(parents=True)
    (private / "agentic-workspace").mkdir(parents=True)
    monkeypatch.setattr(kb_ops, "SHARED_REPOS_DIR", str(shared))
    monkeypatch.setattr(kb_ops, "REPOS_DIR", str(private))

    repo_dir, repo_name, extra_skips = kb_ops._resolve_map_target("agentic-workspace")
    assert repo_dir == str(shared / "agentic-workspace")
    assert repo_name == "agentic-workspace"
    assert extra_skips == set()


def test_resolve_map_target_falls_back_to_private_clone_when_not_shared(tmp_path, monkeypatch):
    shared = tmp_path / "shared-repos"  # exists but has no "agentic-workspace" entry
    shared.mkdir()
    private = tmp_path / "private-repos"
    monkeypatch.setattr(kb_ops, "SHARED_REPOS_DIR", str(shared))
    monkeypatch.setattr(kb_ops, "REPOS_DIR", str(private))

    repo_dir, repo_name, extra_skips = kb_ops._resolve_map_target("agentic-workspace")
    assert repo_dir == str(private / "agentic-workspace")


def test_available_repo_names_merges_and_dedupes_shared_and_private(tmp_path, monkeypatch):
    shared = tmp_path / "shared-repos"
    private = tmp_path / "private-repos"
    (shared / "agentic-workspace").mkdir(parents=True)
    (shared / "aw-console").mkdir(parents=True)
    (private / "agentic-workspace").mkdir(parents=True)  # also privately cloned — dedup
    (private / "some-fork").mkdir(parents=True)
    monkeypatch.setattr(kb_ops, "SHARED_REPOS_DIR", str(shared))
    monkeypatch.setattr(kb_ops, "REPOS_DIR", str(private))

    assert kb_ops._available_repo_names() == ["agentic-workspace", "aw-console", "some-fork"]


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


# --- workspace mapped folders (no repository binding) ------------------------


def test_resolve_map_target_prefers_a_mapped_folder_over_a_same_named_repo(tmp_path, monkeypatch):
    """An explicitly mapped folder is a deliberate user choice; a repo that
    merely happens to share the name must not shadow it."""
    folders = tmp_path / "workspace-folders"
    shared = tmp_path / "shared-repos"
    (folders / "docs").mkdir(parents=True)
    (shared / "docs").mkdir(parents=True)
    monkeypatch.setattr(kb_ops, "MAPPED_FOLDERS_DIR", str(folders))
    monkeypatch.setattr(kb_ops, "SHARED_REPOS_DIR", str(shared))

    repo_dir, repo_name, _ = kb_ops._resolve_map_target("docs")

    assert repo_dir == str(folders / "docs")
    assert repo_name == "docs"


def test_mapped_folder_needs_no_git_repo_and_no_repos_dir(tmp_path, monkeypatch):
    """The whole point: a plain directory, not a checkout, not under repos/."""
    folders = tmp_path / "workspace-folders"
    (folders / "notes").mkdir(parents=True)
    (folders / "notes" / "a.md").write_text("# hi")
    monkeypatch.setattr(kb_ops, "MAPPED_FOLDERS_DIR", str(folders))
    monkeypatch.setattr(kb_ops, "SHARED_REPOS_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr(kb_ops, "REPOS_DIR", str(tmp_path / "also-nope"))

    repo_dir, repo_name, _ = kb_ops._resolve_map_target("notes")

    assert repo_dir == str(folders / "notes")
    assert not (folders / "notes" / ".git").exists()


def test_mapped_folder_names_lists_only_directories(tmp_path, monkeypatch):
    folders = tmp_path / "workspace-folders"
    (folders / "docs").mkdir(parents=True)
    (folders / "datasets").mkdir(parents=True)
    (folders / "stray.txt").write_text("x")
    monkeypatch.setattr(kb_ops, "MAPPED_FOLDERS_DIR", str(folders))

    assert kb_ops._mapped_folder_names() == ["datasets", "docs"]


def test_mapped_folder_names_is_empty_when_the_mount_is_absent(tmp_path, monkeypatch):
    """An older workspace core with no $AW_WORKSPACE_FOLDERS support just
    doesn't create the mount — that must degrade, not raise."""
    monkeypatch.setattr(kb_ops, "MAPPED_FOLDERS_DIR", str(tmp_path / "missing"))

    assert kb_ops._mapped_folder_names() == []


def test_available_repo_names_includes_mapped_folders(tmp_path, monkeypatch):
    folders = tmp_path / "workspace-folders"
    shared = tmp_path / "shared-repos"
    (folders / "docs").mkdir(parents=True)
    (shared / "aw-console").mkdir(parents=True)
    monkeypatch.setattr(kb_ops, "MAPPED_FOLDERS_DIR", str(folders))
    monkeypatch.setattr(kb_ops, "SHARED_REPOS_DIR", str(shared))
    monkeypatch.setattr(kb_ops, "REPOS_DIR", str(tmp_path / "private"))

    assert kb_ops._available_repo_names() == ["aw-console", "docs"]


def test_workspace_state_dir_is_skipped(tmp_path, monkeypatch):
    """Mapping a workspace ROOT must not walk into `.aw-workspace` — that's
    this app's own KB output, its data dir and the shared venv. Reachable
    only since arbitrary folders became mappable, and the BASE_DIR self-map
    guard never fires for it (the root arrives as /workspace-folders/<name>)."""
    assert ".aw-workspace" in kb_ops.SKIP_DIRS

    folders = tmp_path / "workspace-folders"
    root = folders / "aw-workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "real.py").write_text("def keep(): pass\n")
    (root / ".aw-workspace" / "knowledge_base").mkdir(parents=True)
    (root / ".aw-workspace" / "knowledge_base" / "noise.py").write_text("def drop(): pass\n")
    monkeypatch.setattr(kb_ops, "MAPPED_FOLDERS_DIR", str(folders))

    walked = []
    for dirpath, dirs, files in __import__("os").walk(str(root)):
        dirs[:] = [d for d in dirs if d not in kb_ops.SKIP_DIRS]
        walked.extend(files)

    assert "real.py" in walked
    assert "noise.py" not in walked
