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


def get_settings() -> dict:
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("map_paths", [])
    return data


def save_settings(data: dict) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    current = get_settings()
    current.update(data)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(current, f, indent=2)
    return current
