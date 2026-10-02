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
}

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

# --- App / research logging --------------------------------------------------
DB_PATH = ROOT / "data" / "aiprls.sqlite3"
HOST = os.getenv("AIPRLS_HOST", "0.0.0.0")
PORT = int(os.getenv("AIPRLS_PORT", "8000"))

# --- Instructor review ------------------------------------------------------
# Shared secret the instructor types on the "Instructor access" screen.
# Set it on the server (never commit it):  export AIPRLS_INSTRUCTOR_KEY='...'
# Empty = instructor view disabled.
INSTRUCTOR_KEY = os.getenv("AIPRLS_INSTRUCTOR_KEY", "")
