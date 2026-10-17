"""Tiny JSON-file record of which rides have been synced, to avoid re-uploads."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone


class SyncState:
    def __init__(self, path: str):
        self.path = path
        self._synced: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as fh:
                self._synced = json.load(fh).get("synced", {})
        except (FileNotFoundError, ValueError):
            self._synced = {}

    def is_synced(self, ride_id: str) -> bool:
        return ride_id in self._synced

    def mark(self, ride_id: str, status: str) -> None:
        self._synced[ride_id] = {
            "status": status,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._save()

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump({"synced": self._synced}, fh, indent=2)
        os.replace(tmp, self.path)
