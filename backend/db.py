"""SQLite research logging. Everything is keyed by anonymous study IDs.

Tables:
  students  — study IDs and consent timestamp
  messages  — every chat turn, both directions
  sessions  — chapter practice sessions (e.g. 20 questions, then a summary)
  attempts  — every answered practice question, with reasoning + verdict
  feedback  — thumbs up/down on assistant messages
  coaching_notes — instructor guidance for a student, used by the AI tutor
  reasoning_turns — the MKO dialogue on each attempt (prompts, replies, support level)
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
  reflection TEXT,               -- the student's closing reflection, if given
  session_id TEXT                -- chapter session this attempt belongs to (NULL = standalone)
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  study_id TEXT NOT NULL,
  chapter INTEGER,
  total_questions INTEGER NOT NULL,
  started_at REAL NOT NULL,
  completed_at REAL,             -- NULL while in progress
  ended_early INTEGER DEFAULT 0, -- 1 if the student stopped before the last question
  summary TEXT                   -- the summative feedback shown at the end
);
CREATE TABLE IF NOT EXISTS coaching_notes (
  id TEXT PRIMARY KEY,
  study_id TEXT NOT NULL,
  ts REAL NOT NULL,
  note TEXT NOT NULL,            -- the instructor's coaching direction
  attempt_id TEXT,               -- optional: the attempt that prompted it
  active INTEGER DEFAULT 1,      -- 1 = the AI uses it; 0 = retired by instructor
  delivered_at REAL              -- when the student first saw it
);
CREATE TABLE IF NOT EXISTS reasoning_turns (
  id TEXT PRIMARY KEY,
  attempt_id TEXT NOT NULL,
  study_id TEXT NOT NULL,
  turn INTEGER NOT NULL,
  role TEXT NOT NULL,            -- 'tutor' or 'student'
  agent TEXT,                    -- reasoning / expert / patient (tutor turns)
  support_level INTEGER,         -- 0-3 (tutor turns)
  reasoning_quality TEXT,        -- strong / partial / weak (tutor judgement)
  gap TEXT,                      -- knowledge/reasoning gap the tutor identified
  content TEXT NOT NULL,
  ts REAL NOT NULL
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
            "ALTER TABLE attempts ADD COLUMN session_id TEXT",
            "ALTER TABLE attempts ADD COLUMN reasoning_quality TEXT",
            "ALTER TABLE attempts ADD COLUMN probe_turns INTEGER DEFAULT 0",
            "ALTER TABLE attempts ADD COLUMN max_support_level INTEGER",
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
    session_id: str | None = None,
    dialogue: list[dict] | None = None,
) -> str:
    aid = uuid.uuid4().hex
    with _conn() as con:
        con.execute(
            "INSERT INTO attempts (id, study_id, ts, question_json, chapter, domain, "
            "fmt, selected, explanation, verdict, bloom_level, used_scaffold, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                session_id,
            ),
        )
        tutor = [d for d in (dialogue or []) if d["role"] == "tutor"]
        if tutor:
            con.execute(
                "UPDATE attempts SET reasoning_quality=?, probe_turns=?, max_support_level=? WHERE id=?",
                (tutor[0].get("reasoning_quality"), len(tutor),
                 max(d.get("support_level") or 0 for d in tutor), aid),
            )
        for i, d in enumerate(dialogue or [], 1):
            con.execute(
                "INSERT INTO reasoning_turns (id, attempt_id, study_id, turn, role, agent, "
                "support_level, reasoning_quality, gap, content, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, aid, study_id, i, d["role"], d.get("agent"),
                 d.get("support_level"), d.get("reasoning_quality"), d.get("gap"),
                 d["content"], d.get("ts", time.time())),
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
    """Aggregate stats (all of a student's attempts) for the Progress agent."""
    with _conn() as con:
        rows = con.execute(
            "SELECT chapter, domain, fmt, verdict, bloom_level, used_scaffold, "
            "reflection, ts FROM attempts WHERE study_id=? ORDER BY ts",
            (study_id,),
        ).fetchall()
    return _aggregate(rows)


def _aggregate(rows) -> dict:

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


# --- chapter practice sessions ------------------------------------------------

def create_session(study_id: str, chapter: int | None, total: int) -> str:
    sid = uuid.uuid4().hex
    with _conn() as con:
        con.execute(
            "INSERT INTO sessions (id, study_id, chapter, total_questions, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, study_id, chapter, total, time.time()),
        )
    return sid


def complete_session(session_id: str, summary: str, ended_early: bool) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE sessions SET completed_at=?, summary=?, ended_early=? WHERE id=?",
            (time.time(), summary, 1 if ended_early else 0, session_id),
        )


def session_topics(session_id: str) -> list[str]:
    """Topics already used in this session, so the maker doesn't repeat cases."""
    with _conn() as con:
        rows = con.execute(
            "SELECT question_json FROM attempts WHERE session_id=? ORDER BY ts",
            (session_id,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            t = json.loads(r["question_json"]).get("topic", "")
        except json.JSONDecodeError:
            t = ""
        if t:
            out.append(t)
    return out


def session_report(session_id: str) -> dict:
    """Stats for ONE session only (not the student's whole history), plus a
    per-question list the summary agent and the UI can use."""
    with _conn() as con:
        rows = con.execute(
            "SELECT chapter, domain, fmt, verdict, bloom_level, used_scaffold, "
            "reflection, ts, question_json, explanation, reasoning_quality, probe_turns, "
            "max_support_level FROM attempts WHERE session_id=? ORDER BY ts",
            (session_id,),
        ).fetchall()
        sess = con.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    report = _aggregate(rows)
    report["chapter"] = sess["chapter"] if sess else None
    report["planned_questions"] = sess["total_questions"] if sess else None
    report["scaffolded_then_correct"] = sum(
        1 for r in rows if r["used_scaffold"] and r["verdict"] == "correct"
    )
    items = []
    for i, r in enumerate(rows, 1):
        try:
            q = json.loads(r["question_json"])
        except json.JSONDecodeError:
            q = {}
        items.append({
            "number": i,
            "topic": q.get("topic", ""),
            "domain": r["domain"],
            "verdict": r["verdict"],
            "used_scaffold": bool(r["used_scaffold"]),
            "bloom_level": r["bloom_level"],
            "explanation": r["explanation"] or "",
            "reflection": r["reflection"] or "",
            "initial_reasoning": r["reasoning_quality"],
            "tutor_prompts": r["probe_turns"] or 0,
            "max_support_level": r["max_support_level"],
        })
    report["questions"] = items
    levels = [i["max_support_level"] for i in items if i["max_support_level"] is not None]
    report["avg_support_level"] = round(sum(levels) / len(levels), 2) if levels else None
    report["initial_reasoning_counts"] = {
        k: sum(1 for i in items if i["initial_reasoning"] == k) for k in ("strong", "partial", "weak")
    }
    return report


# --- instructor review & coaching ---------------------------------------------

def instructor_students() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """
            SELECT s.study_id, s.consented_at,
                   COUNT(a.id) AS attempts,
                   SUM(CASE WHEN a.verdict='correct' THEN 1 ELSE 0 END) AS correct,
                   SUM(CASE WHEN a.used_scaffold=1 THEN 1 ELSE 0 END) AS scaffolded,
                   SUM(CASE WHEN a.reflection IS NOT NULL AND a.reflection<>'' THEN 1 ELSE 0 END) AS reflections,
                   MAX(a.ts) AS last_attempt,
                   (SELECT COUNT(*) FROM sessions x WHERE x.study_id=s.study_id) AS sessions,
                   (SELECT COUNT(*) FROM coaching_notes n WHERE n.study_id=s.study_id AND n.active=1) AS active_notes
            FROM students s LEFT JOIN attempts a ON a.study_id = s.study_id
            GROUP BY s.study_id ORDER BY COALESCE(MAX(a.ts), s.consented_at) DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def instructor_attempts(study_id: str) -> list[dict]:
    """Everything the instructor needs to review a student's thinking."""
    with _conn() as con:
        rows = con.execute(
            "SELECT id, ts, session_id, chapter, domain, question_json, selected, "
            "explanation, verdict, bloom_level, used_scaffold, reflection, "
            "reasoning_quality, probe_turns, max_support_level "
            "FROM attempts WHERE study_id=? ORDER BY ts DESC",
            (study_id,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            q = json.loads(r["question_json"])
        except json.JSONDecodeError:
            q = {}
        out.append({
            "id": r["id"], "ts": r["ts"], "session_id": r["session_id"],
            "chapter": r["chapter"], "domain": r["domain"],
            "topic": q.get("topic", ""), "stem": q.get("stem", ""),
            "options": q.get("options", []), "correct": q.get("correct", []),
            "selected": json.loads(r["selected"] or "[]"),
            "explanation": r["explanation"], "verdict": r["verdict"],
            "bloom_level": r["bloom_level"], "used_scaffold": bool(r["used_scaffold"]),
            "reflection": r["reflection"],
            "reasoning_quality": r["reasoning_quality"],
            "max_support_level": r["max_support_level"],
            "dialogue": dialogue_for(con_rows=None, attempt_id=r["id"]),
        })
    return out


def dialogue_for(con_rows=None, attempt_id: str = "") -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT role, agent, support_level, reasoning_quality, gap, content FROM reasoning_turns "
            "WHERE attempt_id=? ORDER BY turn", (attempt_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def instructor_sessions(study_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM attempts a WHERE a.session_id=s.id) AS answered, "
            "(SELECT COUNT(*) FROM attempts a WHERE a.session_id=s.id AND a.verdict='correct') AS correct "
            "FROM sessions s WHERE s.study_id=? ORDER BY s.started_at DESC",
            (study_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def add_coaching_note(study_id: str, note: str, attempt_id: str | None = None) -> str:
    nid = uuid.uuid4().hex
    with _conn() as con:
        con.execute(
            "INSERT INTO coaching_notes (id, study_id, ts, note, attempt_id) VALUES (?, ?, ?, ?, ?)",
            (nid, study_id, time.time(), note, attempt_id),
        )
    return nid


def list_coaching_notes(study_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM coaching_notes WHERE study_id=? ORDER BY ts DESC", (study_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def set_coaching_note_active(note_id: str, active: bool) -> None:
    with _conn() as con:
        con.execute("UPDATE coaching_notes SET active=? WHERE id=?", (1 if active else 0, note_id))


def active_guidance(study_id: str) -> list[str]:
    """Active instructor notes, oldest first, for the AI to follow."""
    with _conn() as con:
        rows = con.execute(
            "SELECT note FROM coaching_notes WHERE study_id=? AND active=1 ORDER BY ts",
            (study_id,),
        ).fetchall()
    return [r["note"] for r in rows]


def take_undelivered_note(study_id: str) -> str | None:
    """Oldest active note the student hasn't seen yet; marks it delivered."""
    with _conn() as con:
        row = con.execute(
            "SELECT id, note FROM coaching_notes WHERE study_id=? AND active=1 "
            "AND delivered_at IS NULL ORDER BY ts LIMIT 1",
            (study_id,),
        ).fetchone()
        if not row:
            return None
        con.execute("UPDATE coaching_notes SET delivered_at=? WHERE id=?", (time.time(), row["id"]))
    return row["note"]
