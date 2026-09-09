"""Tests for kb_app/self_register.py — writing this app's own mcp.json entry
so aw-mcp-gateway's app-scan discovers /mcp with no manual wiring.

Currently the least-tested seam in the repo (0% before this pilot card) —
see docs/standards/pipeline-testing.md §3 ("plugin.py .../ self-registration
is the app's contract with the workspace").
"""

from __future__ import annotations

import importlib
import json

from kb_app import self_register


def _reload(monkeypatch, pkg_root):
    monkeypatch.setenv("KB_PKG_ROOT", str(pkg_root))
    importlib.reload(self_register)
    return self_register


def test_register_self_noops_when_pkg_root_missing(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    mod = _reload(monkeypatch, missing)

    mod.register_self(8000)

    assert not missing.exists()


def test_register_self_writes_new_mcp_json(tmp_path, monkeypatch):
    mod = _reload(monkeypatch, tmp_path)
    monkeypatch.setenv("AW_APP_SELF_HOST", "aw-app-kb")

    mod.register_self(8000)

    data = json.loads((tmp_path / "mcp.json").read_text())
    assert data == {
        "mcpServers": {
            "kb": {"type": "http", "url": "http://aw-app-kb:8000/mcp", "enabled": True}
        }
    }


def test_register_self_defaults_host_to_loopback(tmp_path, monkeypatch):
    mod = _reload(monkeypatch, tmp_path)
    monkeypatch.delenv("AW_APP_SELF_HOST", raising=False)

    mod.register_self(9000)

    data = json.loads((tmp_path / "mcp.json").read_text())
    assert data["mcpServers"]["kb"]["url"] == "http://127.0.0.1:9000/mcp"


def test_register_self_merges_with_existing_entries(tmp_path, monkeypatch):
    mod = _reload(monkeypatch, tmp_path)
    (tmp_path / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "other-app": {
                        "type": "http",
                        "url": "http://x:1/mcp",
                        "enabled": True,
                    }
                }
            }
        )
    )

    mod.register_self(8000)

    data = json.loads((tmp_path / "mcp.json").read_text())
    assert "other-app" in data["mcpServers"]
    assert "kb" in data["mcpServers"]


def test_register_self_recovers_from_corrupt_existing_file(tmp_path, monkeypatch):
    mod = _reload(monkeypatch, tmp_path)
    (tmp_path / "mcp.json").write_text("{not valid json")

    mod.register_self(8000)

    data = json.loads((tmp_path / "mcp.json").read_text())
    assert "kb" in data["mcpServers"]


def test_register_self_is_a_noop_write_when_entry_already_current(
    tmp_path, monkeypatch, caplog
):
    mod = _reload(monkeypatch, tmp_path)
    mod.register_self(8000)
    written_first = (tmp_path / "mcp.json").read_text()

    caplog.clear()
    mod.register_self(8000)

    assert (tmp_path / "mcp.json").read_text() == written_first
    assert "registered self" not in caplog.text


def test_register_self_logs_warning_on_write_failure(tmp_path, monkeypatch, caplog):
    mod = _reload(monkeypatch, tmp_path)
    # tmp_path itself as MCP_JSON_PATH's replace target directory removed
    # mid-flight isn't easy to simulate portably; instead point the tmp file
    # write at a path whose parent doesn't exist to force an OSError.
    monkeypatch.setattr(
        mod, "MCP_JSON_PATH", str(tmp_path / "nested" / "missing" / "mcp.json")
    )

    import logging

    with caplog.at_level(logging.WARNING):
        mod.register_self(8000)

    assert "could not write" in caplog.text
