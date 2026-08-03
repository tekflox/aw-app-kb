"""Write this app's own ``mcp.json`` so aw-mcp-gateway's app-scan
(``scan_app_mcp_servers()``, reading ``<installed-app-dir>/mcp.json``)
discovers the ``/mcp`` endpoint below without any manual wiring — the
``contributes.mcp.reload_on_save`` mechanism aw-workspace already has for
this (see aw-mcp-gateway's own ``config.py::register_self_in_host_mcp_json``
for the sibling case: a container writing ITS OWN entry into the file a
different process reads).

127.0.0.1 would resolve inside THIS container's own netns, not from the
gateway's sibling container — same reasoning as aw-mcp-gateway's fix.
AW_APP_SELF_HOST (aw-workspace's own env, injected unconditionally by
ContainerSupervisor.start() whenever the app is on the shared podman
network) is the name the gateway actually reaches this container by.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("aw-app-kb")

# Package root — mounted read-write via aw-app.json's "source": "." volume,
# sibling to aw-app.json itself, exactly where the gateway's scan expects
# an installed app's mcp.json to live.
PKG_ROOT = os.environ.get("KB_PKG_ROOT", "/app/pkg")
MCP_JSON_PATH = os.path.join(PKG_ROOT, "mcp.json")


def register_self(port: int) -> None:
    """Best-effort; a bare `python -m kb_app` dev run with no aw-workspace
    volume mount has nowhere to write this and simply no-ops."""
    if not os.path.isdir(PKG_ROOT):
        return
    host = os.environ.get("AW_APP_SELF_HOST", "127.0.0.1")
    entry = {"type": "http", "url": f"http://{host}:{port}/mcp", "enabled": True}

    data = {"mcpServers": {}}
    try:
        with open(MCP_JSON_PATH) as f:
            existing = json.load(f)
        if isinstance(existing, dict) and isinstance(existing.get("mcpServers"), dict):
            data = existing
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    if data["mcpServers"].get("kb") == entry:
        return
    data["mcpServers"]["kb"] = entry
    try:
        tmp = f"{MCP_JSON_PATH}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, MCP_JSON_PATH)
        log.info("registered self as 'kb' in %s (%s)", MCP_JSON_PATH, entry["url"])
    except OSError as e:
        log.warning("could not write %s: %s", MCP_JSON_PATH, e)
