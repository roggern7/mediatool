from __future__ import annotations

import json
from typing import Any

from app.db.connection import Database, utc_now


class SettingsRepository:
    """Tabelas `settings` (valores JSON) e `app_state` (texto). Nunca recebem segredos."""

    def __init__(self, db: Database):
        self.db = db

    def all(self) -> dict[str, Any]:
        return {row["key"]: json.loads(row["value"]) for row in self.db.fetchall("SELECT key, value FROM settings")}

    def get(self, key: str, default: Any = None) -> Any:
        value = self.db.scalar("SELECT value FROM settings WHERE key = ?", (key,))
        return default if value is None else json.loads(value)

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value), utc_now()),
        )

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM settings WHERE key = ?", (key,))

    def get_state(self, key: str) -> str | None:
        return self.db.scalar("SELECT value FROM app_state WHERE key = ?", (key,))

    def set_state(self, key: str, value: str | None) -> None:
        self.db.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, utc_now()),
        )
