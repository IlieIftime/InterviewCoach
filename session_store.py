"""Small SQLite-backed session store for the local MVP."""
from __future__ import annotations

import json
import sqlite3
import time
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
            migrations = {
                "language": "TEXT NOT NULL DEFAULT 'pt-PT'",
                "must": "TEXT NOT NULL DEFAULT ''",
                "dont": "TEXT NOT NULL DEFAULT ''",
                "questions_to_ask": "TEXT NOT NULL DEFAULT '[]'",
                "questions_to_avoid": "TEXT NOT NULL DEFAULT '[]'",
                "script_fidelity": "TEXT NOT NULL DEFAULT 'adaptive'",
                "duration_minutes": "INTEGER NOT NULL DEFAULT 20",
                "min_pause_seconds": "REAL NOT NULL DEFAULT 1.5",
                "model": "TEXT NOT NULL DEFAULT ''",
                "started_at": "REAL NOT NULL DEFAULT 0",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_metrics (
                    metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    turn_index INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    stt_ms REAL,
                    llm_ms REAL,
                    tts_ms REAL,
                    answer_chars INTEGER,
                    stt_confidence REAL,
                    capture_mode TEXT,
                    auto_submitted INTEGER NOT NULL DEFAULT 0
                )
                """
            )

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
                    language, must, dont, questions_to_ask, questions_to_avoid,
                    script_fidelity, duration_minutes, min_pause_seconds, model, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    questions_to_ask=excluded.questions_to_ask,
                    questions_to_avoid=excluded.questions_to_avoid,
                    script_fidelity=excluded.script_fidelity,
                    duration_minutes=excluded.duration_minutes,
                    min_pause_seconds=excluded.min_pause_seconds,
                    model=excluded.model,
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
                    session.must,
                    session.dont,
                    json.dumps(session.questions_to_ask, ensure_ascii=False),
                    json.dumps(session.questions_to_avoid, ensure_ascii=False),
                    session.script_fidelity,
                    session.duration_minutes,
                    session.min_pause_seconds,
                    session.model,
                    session.started_at,
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
            language=row["language"] or "pt-PT",
            must=row["must"] or "",
            dont=row["dont"] or "",
            questions_to_ask=json.loads(row["questions_to_ask"] or "[]"),
            questions_to_avoid=json.loads(row["questions_to_avoid"] or "[]"),
            script_fidelity=row["script_fidelity"] or "adaptive",
            duration_minutes=int(row["duration_minutes"] or 20),
            min_pause_seconds=float(row["min_pause_seconds"] or 1.5),
            model=row["model"] or "",
            started_at=float(row["started_at"] or 0) or time.time(),
        )

    def record_metric(self, session_id: str, event: str, turn_index: int = 0, **values: Any) -> None:
        allowed = {
            "stt_ms", "llm_ms", "tts_ms", "answer_chars", "stt_confidence",
            "capture_mode", "auto_submitted",
        }
        payload = {key: values.get(key) for key in allowed}
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO session_metrics (
                    session_id, event, turn_index, created_at, stt_ms, llm_ms,
                    tts_ms, answer_chars, stt_confidence, capture_mode, auto_submitted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    event,
                    turn_index,
                    time.time(),
                    payload["stt_ms"],
                    payload["llm_ms"],
                    payload["tts_ms"],
                    payload["answer_chars"],
                    payload["stt_confidence"],
                    payload["capture_mode"],
                    int(bool(payload["auto_submitted"])),
                ),
            )

    def metrics(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM session_metrics WHERE session_id = ? ORDER BY metric_id",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
