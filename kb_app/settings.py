"""Local app settings (persisted volume), the decoupled-app replacement for
reading ``knowledge_base.map_paths`` out of the monolith's shared aw.json.

One small JSON file under the persistent data volume — the whole settings
surface today is just ``map_paths``, so a dedicated file beats inventing a
generic config store for one key.
"""

from __future__ import annotations

import json
import os

DATA_DIR = os.environ.get("KB_DATA_DIR", "/app/data")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")

#: Seeded on a FRESH install only (no settings file yet). The workspace's
#: repos/ dir is the one thing worth indexing that the workspace folder map
#: does not already cover, and it is mounted here as $AW_WORKSPACE_REPOS, so
#: a new workspace gets a useful KB without anyone configuring anything.
#:
#: Only a default: once the file exists this is never re-applied, so removing
#: the entry in the UI sticks.
DEFAULT_MAP_PATHS = [
    os.path.join(os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace"), "repos"),
]


def get_settings() -> dict:
    fresh = False
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
        fresh = True
    data.setdefault("map_paths", list(DEFAULT_MAP_PATHS) if fresh else [])
    # Workspace folders the user switched OFF. Stored as an opt-OUT so the
    # workspace stays the source of truth for what EXISTS: a folder mapped
    # later is indexed by default, and this file only ever records the
    # exceptions. An opt-in list would silently ignore new folders.
    data.setdefault("disabled_folders", [])
    return data


def save_settings(data: dict) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    current = get_settings()
    current.update(data)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(current, f, indent=2)
    return current
