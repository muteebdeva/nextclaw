"""Structured, searchable long-term memory with provenance.

MVP storage: SQLite with keyword search. Each entry records text, tags,
source (provenance), and timestamps. Entries can be edited/deleted by id.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class MemoryEntry:
    id: int
    text: str
    tags: list
    source: str
    created_at: float
    updated_at: float


class MemoryStore:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "memory.db")
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL DEFAULT 'agent',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_updated ON entries(updated_at)")

    def remember(self, text: str, tags: Optional[List[str]] = None, source: str = "agent") -> int:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO entries (text, tags_json, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (text, json.dumps(list(tags or [])), source, now, now),
            )
            return int(cur.lastrowid)

    def update(self, entry_id: int, text: Optional[str] = None, tags: Optional[List[str]] = None) -> bool:
        fields = []
        values: list = []
        if text is not None:
            fields.append("text = ?")
            values.append(text)
        if tags is not None:
            fields.append("tags_json = ?")
            values.append(json.dumps(list(tags)))
        if not fields:
            return False
        fields.append("updated_at = ?")
        values.append(time.time())
        values.append(entry_id)
        with self._connect() as conn:
            cur = conn.execute(f"UPDATE entries SET {', '.join(fields)} WHERE id = ?", values)
            return cur.rowcount > 0

    def forget(self, entry_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            return cur.rowcount > 0

    def get(self, entry_id: int) -> Optional[MemoryEntry]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
            return self._row_to_entry(row) if row else None

    def recall(self, query: str, limit: int = 5) -> List[MemoryEntry]:
        like = f"%{query}%"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM entries
                WHERE text LIKE ? OR tags_json LIKE ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (like, like, limit),
            ).fetchall()
            return [self._row_to_entry(r) for r in rows]

    def list_entries(self, limit: int = 50) -> List[MemoryEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM entries ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_entry(r) for r in rows]

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> MemoryEntry:
        return MemoryEntry(
            id=int(row["id"]),
            text=row["text"],
            tags=json.loads(row["tags_json"]),
            source=row["source"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )
