"""Central configuration for AI-PRLS. Every value can be overridden with an env var."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- LLM serving (vLLM exposes an OpenAI-compatible API) ---------------------
LLM_BASE_URL = os.getenv("AIPRLS_LLM_URL", "http://localhost:8001/v1")
LLM_MODEL = os.getenv("AIPRLS_LLM_MODEL", "Qwen/Qwen2.5-14B-Instruct")
LLM_API_KEY = os.getenv("AIPRLS_LLM_KEY", "not-needed-for-local")
MOCK_LLM = os.getenv("AIPRLS_MOCK_LLM", "0") == "1"   # run the UI without GPUs

# Generation settings per agent role
GEN = {
    "router":   {"temperature": 0.0, "max_tokens": 200},
    "question": {"temperature": 0.6, "max_tokens": 1600},
    "scaffold": {"temperature": 0.5, "max_tokens": 150},
    "coach":    {"temperature": 0.4, "max_tokens": 900},
    "explain":  {"temperature": 0.5, "max_tokens": 900},
    "progress": {"temperature": 0.4, "max_tokens": 500},
    "chat":     {"temperature": 0.5, "max_tokens": 400},
    "summary":  {"temperature": 0.4, "max_tokens": 900},
    "probe":    {"temperature": 0.5, "max_tokens": 300},
    "solver":   {"temperature": 0.0, "max_tokens": 200},
    "open":     {"temperature": 0.6, "max_tokens": 1200},
}

# Timed quiz (Dr. Dumitrescu, Oct 29): the chapter session is a timed,
# exam-style quiz with a performance report at the end. Default pace matches
# the real exam (about 4 hours for 170 items, roughly 85 seconds per item).
QUIZ_SECONDS_PER_QUESTION = int(os.getenv("AIPRLS_QUIZ_SECONDS_PER_QUESTION", "85"))
# How many questions to write ahead in the background so "Next" is instant.
QUIZ_PREFETCH = int(os.getenv("AIPRLS_QUIZ_PREFETCH", "2"))

# Open chat ("like ChatGPT", Dr. Dumitrescu): how many earlier turns of the
# conversation the model sees with each new message.
OPEN_CHAT_HISTORY = int(os.getenv("AIPRLS_OPEN_CHAT_HISTORY", "16"))

# --- Chapter practice sessions ("select a chapter, 20 questions, summary") ---
SESSION_LENGTH = int(os.getenv("AIPRLS_SESSION_LENGTH", "20"))
CHAPTERS = int(os.getenv("AIPRLS_CHAPTERS", "16"))   # TherapyEd chapters offered
# Chapter 1 is an overview of the exam process, not clinical content, so it is
# not offered for practice questions (Dr. Dumitrescu, Oct 1).
FIRST_PRACTICE_CHAPTER = int(os.getenv("AIPRLS_FIRST_PRACTICE_CHAPTER", "2"))
MAX_SESSION_LENGTH = 50
# Dr. Dumitrescu's 5-week TherapyEd review plan: chapter -> week (dropdown labels).
STUDY_PLAN_WEEKS = {3: 1, 4: 1, 6: 2, 8: 2, 11: 2, 7: 3, 9: 3, 12: 3,
                    10: 4, 13: 4, 14: 4, 5: 5, 15: 5}

# --- RAG over the team's own companion documents -----------------------------
COMPANION_DIR = ROOT / "companion_docs"
INDEX_PATH = ROOT / "data" / "companion_index.npz"
EMBED_MODEL = os.getenv("AIPRLS_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
EMBED_DEVICE = os.getenv("AIPRLS_EMBED_DEVICE", "cpu")  # set "cuda:2" to use the spare GPU
TOP_K = int(os.getenv("AIPRLS_TOP_K", "4"))
CHUNK_CHARS = 1200

# --- E-book (the team's licensed copy; kept on the server only) --------------
# Put the file in ebook/ (git-ignored) and run: python scripts/import_ebook.py
# The app then writes every question from a retrieved book passage and records
# its chapter / section / pages as the question's source.
BOOK_DIR = ROOT / "ebook"
BOOK_CHUNKS_PATH = ROOT / "data" / "book_chunks.jsonl"
BOOK_INDEX_PATH = ROOT / "data" / "book_index.npz"
BOOK_CHUNK_CHARS = int(os.getenv("AIPRLS_BOOK_CHUNK_CHARS", "1500"))

# --- Adaptive difficulty ----------------------------------------------------
# A correct answer moves the next question up a level, a wrong one down a
# level; the next session starts at a level set by this session's score.
LEVELS = {1: "Foundation", 2: "Standard", 3: "Challenge"}
# Off for now (Prof. Xi, Nov 19: the team still has to define what they want).
# Turn on with AIPRLS_ADAPTIVE=1; until then every question is "Standard".
ADAPTIVE = os.getenv("AIPRLS_ADAPTIVE", "0") == "1"
LEVEL_UP_SCORE = float(os.getenv("AIPRLS_LEVEL_UP_SCORE", "0.8"))
LEVEL_DOWN_SCORE = float(os.getenv("AIPRLS_LEVEL_DOWN_SCORE", "0.5"))

# --- App / research logging --------------------------------------------------
DB_PATH = ROOT / "data" / "aiprls.sqlite3"
HOST = os.getenv("AIPRLS_HOST", "0.0.0.0")
PORT = int(os.getenv("AIPRLS_PORT", "8000"))

# --- Instructor review ------------------------------------------------------
# Shared secret the instructor types on the "Instructor access" screen.
# Set it on the server (never commit it):  export AIPRLS_INSTRUCTOR_KEY='...'
# Empty = instructor view disabled.
INSTRUCTOR_KEY = os.getenv("AIPRLS_INSTRUCTOR_KEY", "")
