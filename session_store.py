"""Small SQLite-backed session store for the local MVP."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Type


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
                    questions, current_index, history, pending_question
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    persona=excluded.persona,
                    instructions=excluded.instructions,
                    avoid=excluded.avoid,
                    candidate_context=excluded.candidate_context,
                    questions=excluded.questions,
                    current_index=excluded.current_index,
                    history=excluded.history,
                    pending_question=excluded.pending_question
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
                ),
            )

    def get(self, session_id: str, session_type: Type[Any]) -> Any | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        return session_type(
            persona=row["persona"],
            instructions=row["instructions"],
            avoid=row["avoid"],
            candidate_context=row["candidate_context"],
            questions=json.loads(row["questions"]),
            current_index=row["current_index"],
            history=json.loads(row["history"]),
            pending_question=row["pending_question"],
        )

    def delete(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
