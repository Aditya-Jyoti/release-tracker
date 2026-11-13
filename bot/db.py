"""SQLite storage.

Uses stdlib ``sqlite3`` synchronously. All calls are tiny single-row/indexed
queries on a local file, so they don't meaningfully block the event loop, and
everything runs on the event-loop thread (no cross-thread connection use).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Each entry migrates from user_version == index to index + 1. Append only.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE tracked_items (
        id              INTEGER PRIMARY KEY,
        chat_id         INTEGER NOT NULL,
        kind            TEXT    NOT NULL,
        target          TEXT    NOT NULL,
        options         TEXT    NOT NULL DEFAULT '{}',
        last_version    TEXT,
        last_checked_at TEXT,
        last_changed_at TEXT,
        created_at      TEXT    NOT NULL,
        UNIQUE (chat_id, kind, target)
    );
    CREATE INDEX idx_tracked_items_kind_target ON tracked_items (kind, target);
    """,
]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class TrackedItem:
    id: int
    chat_id: int
    kind: str
    target: str
    options: dict[str, Any]
    last_version: str | None
    last_checked_at: str | None
    last_changed_at: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> TrackedItem:
        return cls(
            id=row["id"],
            chat_id=row["chat_id"],
            kind=row["kind"],
            target=row["target"],
            options=json.loads(row["options"] or "{}"),
            last_version=row["last_version"],
            last_checked_at=row["last_checked_at"],
            last_changed_at=row["last_changed_at"],
            created_at=row["created_at"],
        )


class Database:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; explicit txns below
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for i in range(current, len(MIGRATIONS)):
            self._conn.executescript(f"BEGIN; {MIGRATIONS[i]}; PRAGMA user_version = {i + 1}; COMMIT;")

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    # ----------------------------------------------------------------- queries

    def get_item(self, chat_id: int, kind: str, target: str) -> TrackedItem | None:
        row = self._conn.execute(
            "SELECT * FROM tracked_items WHERE chat_id = ? AND kind = ? AND target = ?",
            (chat_id, kind, target),
        ).fetchone()
        return TrackedItem.from_row(row) if row else None

    def list_items(self, chat_id: int | None = None) -> list[TrackedItem]:
        if chat_id is None:
            rows = self._conn.execute("SELECT * FROM tracked_items ORDER BY kind, target, chat_id")
        else:
            rows = self._conn.execute(
                "SELECT * FROM tracked_items WHERE chat_id = ? ORDER BY kind, target", (chat_id,)
            )
        return [TrackedItem.from_row(r) for r in rows]

    def distinct_targets(self) -> list[tuple[str, str]]:
        rows = self._conn.execute("SELECT DISTINCT kind, target FROM tracked_items ORDER BY kind, target")
        return [(r["kind"], r["target"]) for r in rows]

    # ----------------------------------------------------------------- writes

    def upsert_item(
        self,
        chat_id: int,
        kind: str,
        target: str,
        options: Mapping[str, Any],
        last_version: str | None,
    ) -> tuple[TrackedItem, bool]:
        """Insert or update (options + baseline). Returns ``(item, created)``."""
        existed = self.get_item(chat_id, kind, target) is not None
        now = _now()
        self._conn.execute(
            """
            INSERT INTO tracked_items (chat_id, kind, target, options, last_version, last_checked_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chat_id, kind, target) DO UPDATE SET
                options = excluded.options,
                last_version = excluded.last_version,
                last_checked_at = excluded.last_checked_at
            """,
            (chat_id, kind, target, json.dumps(dict(options), sort_keys=True), last_version, now, now),
        )
        item = self.get_item(chat_id, kind, target)
        assert item is not None
        return item, not existed

    def remove_item(self, chat_id: int, kind: str, target: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM tracked_items WHERE chat_id = ? AND kind = ? AND target = ?",
            (chat_id, kind, target),
        )
        return cur.rowcount > 0

    def set_version(self, item_id: int, version: str) -> None:
        now = _now()
        self._conn.execute(
            "UPDATE tracked_items SET last_version = ?, last_changed_at = ?, last_checked_at = ? WHERE id = ?",
            (version, now, now, item_id),
        )

    def mark_checked(self, item_ids: Iterable[int]) -> None:
        ids = list(item_ids)
        if not ids:
            return
        now = _now()
        self._conn.executemany(
            "UPDATE tracked_items SET last_checked_at = ? WHERE id = ?", [(now, i) for i in ids]
        )
