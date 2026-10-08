"""Small SQLite-backed session store for the local MVP."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Type

# Columns added after the original schema. Each tuple is (name, sql_type, default_sql).
# New columns are applied with ALTER TABLE ... ADD COLUMN so existing sessions.db files
# upgrade in place without losing data.
EXTRA_SESSION_COLUMNS: list[tuple[str, str, str]] = [
    ("language", "TEXT", "'pt-PT'"),
    ("must", "TEXT", "'[]'"),
    ("dont", "TEXT", "'[]'"),
    ("questions_to_avoid", "TEXT", "'[]'"),
    ("script_fidelity", "REAL", "0.5"),
    ("duration_minutes", "INTEGER", "30"),
    ("min_pause_seconds", "REAL", "0.8"),
    ("started_at", "REAL", "0"),
]


class SessionStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    persona TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    avoid TEXT NOT NULL,
                    candidate_context TEXT NOT NULL,
                    questions TEXT NOT NULL,
                    current_index INTEGER NOT NULL,
                    history TEXT NOT NULL,
                    pending_question TEXT
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
            if "pending_question" not in columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN pending_question TEXT")
            for name, sql_type, default_sql in EXTRA_SESSION_COLUMNS:
                if name not in columns:
                    connection.execute(f"ALTER TABLE sessions ADD COLUMN {name} {sql_type} DEFAULT {default_sql}")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    latency_ms REAL NOT NULL,
                    meta TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_metrics_session ON metrics(session_id)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def save(self, session_id: str, session: Session) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id, persona, instructions, avoid, candidate_context,
                    questions, current_index, history, pending_question,
                    language, must, dont, questions_to_avoid,
                    script_fidelity, duration_minutes, min_pause_seconds, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    persona=excluded.persona,
                    instructions=excluded.instructions,
                    avoid=excluded.avoid,
                    candidate_context=excluded.candidate_context,
                    questions=excluded.questions,
                    current_index=excluded.current_index,
                    history=excluded.history,
                    pending_question=excluded.pending_question,
                    language=excluded.language,
                    must=excluded.must,
                    dont=excluded.dont,
                    questions_to_avoid=excluded.questions_to_avoid,
                    script_fidelity=excluded.script_fidelity,
                    duration_minutes=excluded.duration_minutes,
                    min_pause_seconds=excluded.min_pause_seconds,
                    started_at=excluded.started_at
                """,
                (
                    session_id,
                    session.persona,
                    session.instructions,
                    session.avoid,
                    session.candidate_context,
                    json.dumps(session.questions, ensure_ascii=False),
                    session.current_index,
                    json.dumps(session.history, ensure_ascii=False),
                    session.pending_question,
                    session.language,
                    json.dumps(session.must, ensure_ascii=False),
                    json.dumps(session.dont, ensure_ascii=False),
                    json.dumps(session.questions_to_avoid, ensure_ascii=False),
                    session.script_fidelity,
                    session.duration_minutes,
                    session.min_pause_seconds,
                    session.started_at,
                ),
            )

    def get(self, session_id: str, session_type: Type[Any]) -> Any | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        row_keys = row.keys()
        return session_type(
            persona=row["persona"],
            instructions=row["instructions"],
            avoid=row["avoid"],
            candidate_context=row["candidate_context"],
            questions=json.loads(row["questions"]),
            current_index=row["current_index"],
            history=json.loads(row["history"]),
            pending_question=row["pending_question"],
            language=row["language"] if "language" in row_keys and row["language"] else "pt-PT",
            must=json.loads(row["must"]) if "must" in row_keys and row["must"] else [],
            dont=json.loads(row["dont"]) if "dont" in row_keys and row["dont"] else [],
            questions_to_avoid=json.loads(row["questions_to_avoid"]) if "questions_to_avoid" in row_keys and row["questions_to_avoid"] else [],
            script_fidelity=row["script_fidelity"] if "script_fidelity" in row_keys and row["script_fidelity"] is not None else 0.5,
            duration_minutes=row["duration_minutes"] if "duration_minutes" in row_keys and row["duration_minutes"] else 30,
            min_pause_seconds=row["min_pause_seconds"] if "min_pause_seconds" in row_keys and row["min_pause_seconds"] is not None else 0.8,
            started_at=row["started_at"] if "started_at" in row_keys and row["started_at"] else time.time(),
        )

    def delete(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])

    def log_metric(self, session_id: str, stage: str, latency_ms: float, meta: dict[str, Any] | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO metrics (session_id, stage, latency_ms, meta, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, stage, latency_ms, json.dumps(meta, ensure_ascii=False) if meta else None, time.time()),
            )

    def get_metrics(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT stage, latency_ms, meta, created_at FROM metrics WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        return [
            {
                "stage": row["stage"],
                "latency_ms": row["latency_ms"],
                "meta": json.loads(row["meta"]) if row["meta"] else None,
                "created_at": row["created_at"],
            }
            for row in rows
        ]
