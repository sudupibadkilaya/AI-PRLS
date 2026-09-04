"""SQLite research logging. Everything is keyed by anonymous study IDs.

Tables:
  students  — study IDs and consent timestamp
  messages  — every chat turn, both directions
  attempts  — every answered practice question, with reasoning + verdict
  feedback  — thumbs up/down on assistant messages
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
  study_id TEXT PRIMARY KEY,
  consented_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  study_id TEXT NOT NULL,
  ts REAL NOT NULL,
  role TEXT NOT NULL,            -- 'student' or 'assistant'
  route TEXT,                    -- which agent handled it
  content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY,
  study_id TEXT NOT NULL,
  ts REAL NOT NULL,
  question_json TEXT NOT NULL,
  chapter INTEGER,
  domain INTEGER,
  fmt TEXT,
  selected TEXT NOT NULL,        -- JSON list of chosen indices
  explanation TEXT,              -- the student's stated reasoning
  verdict TEXT,                  -- correct / partly / incorrect
  bloom_level TEXT,              -- Bloom's level the explanation demonstrated
  used_scaffold INTEGER DEFAULT 0, -- 1 if the student needed a scaffold hint + reconsider
  reflection TEXT                -- the student's closing reflection, if given
);
CREATE TABLE IF NOT EXISTS feedback (
  message_id TEXT PRIMARY KEY,
  study_id TEXT NOT NULL,
  ts REAL NOT NULL,
  rating INTEGER NOT NULL        -- 1 = helpful, -1 = not helpful
);
"""


@contextmanager
def _conn():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> None:
    with _conn() as con:
        con.executescript(_SCHEMA)
        for stmt in (
            "ALTER TABLE attempts ADD COLUMN bloom_level TEXT",
            "ALTER TABLE attempts ADD COLUMN used_scaffold INTEGER DEFAULT 0",
            "ALTER TABLE attempts ADD COLUMN reflection TEXT",
        ):
            try:
                con.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists


def register_student(study_id: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO students (study_id, consented_at) VALUES (?, ?)",
            (study_id, time.time()),
        )


def log_message(study_id: str, role: str, content: str, route: str | None = None) -> str:
    mid = uuid.uuid4().hex
    with _conn() as con:
        con.execute(
            "INSERT INTO messages (id, study_id, ts, role, route, content) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mid, study_id, time.time(), role, route, content),
        )
    return mid


def log_attempt(
    study_id: str,
    question: dict,
    selected: list[int],
    explanation: str,
    verdict: str,
    bloom_level: str | None = None,
    used_scaffold: bool = False,
) -> str:
    aid = uuid.uuid4().hex
    with _conn() as con:
        con.execute(
            "INSERT INTO attempts (id, study_id, ts, question_json, chapter, domain, "
            "fmt, selected, explanation, verdict, bloom_level, used_scaffold) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                aid,
                study_id,
                time.time(),
                json.dumps(question),
                question.get("chapter"),
                question.get("domain"),
                question.get("format"),
                json.dumps(selected),
                explanation,
                verdict,
                bloom_level,
                1 if used_scaffold else 0,
            ),
        )
    return aid


def update_reflection(attempt_id: str, text: str) -> None:
    with _conn() as con:
        con.execute("UPDATE attempts SET reflection=? WHERE id=?", (text, attempt_id))


def log_feedback(study_id: str, message_id: str, rating: int) -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO feedback (message_id, study_id, ts, rating) "
            "VALUES (?, ?, ?, ?)",
            (message_id, study_id, time.time(), rating),
        )


def recent_topics(study_id: str, n: int = 5) -> list[str]:
    """Topics of the student's recent questions, so the maker avoids repeats."""
    with _conn() as con:
        rows = con.execute(
            "SELECT question_json FROM attempts WHERE study_id=? ORDER BY ts DESC LIMIT ?",
            (study_id, n),
        ).fetchall()
    topics = []
    for row in rows:
        try:
            topics.append(json.loads(row["question_json"]).get("topic", ""))
        except json.JSONDecodeError:
            pass
    return [t for t in topics if t]


def stats(study_id: str) -> dict:
    """Aggregate stats used by the Progress agent and the dashboard."""
    with _conn() as con:
        rows = con.execute(
            "SELECT chapter, domain, fmt, verdict, bloom_level, used_scaffold, "
            "reflection, ts FROM attempts WHERE study_id=?",
            (study_id,),
        ).fetchall()

    def bucket(key):
        out: dict = {}
        for r in rows:
            k = r[key] if r[key] is not None else "unknown"
            d = out.setdefault(str(k), {"attempts": 0, "correct": 0})
            d["attempts"] += 1
            if r["verdict"] == "correct":
                d["correct"] += 1
        return out

    total = len(rows)
    correct = sum(1 for r in rows if r["verdict"] == "correct")
    last10 = rows[-10:]
    return {
        "total_attempts": total,
        "total_correct": correct,
        "accuracy": round(correct / total, 2) if total else None,
        "recent10_accuracy": (
            round(sum(1 for r in last10 if r["verdict"] == "correct") / len(last10), 2)
            if last10
            else None
        ),
        "by_chapter": bucket("chapter"),
        "by_domain": bucket("domain"),
        "by_bloom_level": bucket("bloom_level"),
        "independent_rate": (
            round(sum(1 for r in rows if not r["used_scaffold"]) / total, 2) if total else None
        ),
        "reflections_completed": sum(1 for r in rows if r["reflection"]),
    }


def recent_messages(study_id: str, n: int = 8) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT role, content FROM messages WHERE study_id=? ORDER BY ts DESC LIMIT ?",
            (study_id, n),
        ).fetchall()
    return [
        {"role": "user" if r["role"] == "student" else "assistant", "content": r["content"]}
        for r in reversed(rows)
    ]
