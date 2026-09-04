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
    "question": {"temperature": 0.8, "max_tokens": 1600},
    "scaffold": {"temperature": 0.5, "max_tokens": 150},
    "coach":    {"temperature": 0.4, "max_tokens": 900},
    "explain":  {"temperature": 0.5, "max_tokens": 900},
    "progress": {"temperature": 0.4, "max_tokens": 500},
    "chat":     {"temperature": 0.5, "max_tokens": 400},
}

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
