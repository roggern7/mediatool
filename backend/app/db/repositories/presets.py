from __future__ import annotations

import sqlite3
from typing import Any

from app.db.connection import Database, utc_now
from app.db.repositories.common import canonical_json, load_json, row_dict


class DuplicateNameError(Exception):
    pass


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    data = row_dict(row)
    if data is not None:
        data["params"] = load_json(data["params"])
        data["is_builtin"] = bool(data["is_builtin"])
    return data


class PresetRepository:
    def __init__(self, db: Database):
        self.db = db

    def list(self) -> list[dict[str, Any]]:
        rows = self.db.fetchall("SELECT * FROM presets ORDER BY is_builtin DESC, id")
        return [_decode(row) for row in rows]

    def get(self, preset_id: int) -> dict[str, Any] | None:
        return _decode(self.db.fetchone("SELECT * FROM presets WHERE id = ?", (preset_id,)))

    def name_exists(self, name: str, *, exclude_id: int | None = None) -> bool:
        row = self.db.fetchone(
            "SELECT 1 FROM presets WHERE name = ? COLLATE NOCASE AND id IS NOT ?", (name, exclude_id)
        )
        return row is not None

    def first_builtin_id(self) -> int | None:
        return self.db.scalar("SELECT id FROM presets WHERE is_builtin = 1 ORDER BY id LIMIT 1")

    def create(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        try:
            cursor = self.db.execute(
                "INSERT INTO presets (name, is_builtin, params, created_at, updated_at) VALUES (?, 0, ?, ?, ?)",
                (name, canonical_json(params), now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateNameError(name) from exc
        return self.get(cursor.lastrowid)

    def update(self, preset_id: int, *, name: str | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        current = self.get(preset_id)
        if current is None:
            raise KeyError(preset_id)
        new_name = current["name"] if name is None else name
        new_params = current["params"] if params is None else params
        try:
            self.db.execute(
                "UPDATE presets SET name = ?, params = ?, updated_at = ? WHERE id = ?",
                (new_name, canonical_json(new_params), utc_now(), preset_id),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateNameError(new_name) from exc
        return self.get(preset_id)

    def delete(self, preset_id: int) -> None:
        self.db.execute("DELETE FROM presets WHERE id = ? AND is_builtin = 0", (preset_id,))
