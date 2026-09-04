"""Thin client for the local vLLM server (OpenAI-compatible chat API).

Includes a MOCK mode (AIPRLS_MOCK_LLM=1) that serves a small library of
worked examples (backend/mock_examples.json) so the frontend, database, and
demo flow can be exercised on a laptop with no GPUs. These are original
illustrative examples the team wrote for this purpose — not textbook text
and not live model output.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from . import config


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` fences if the model added them."""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def extract_json(text: str) -> dict:
    """Best-effort extraction of a single JSON object from model output."""
    text = _strip_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # fall back to the outermost {...}
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"No JSON object found in model output: {text[:200]}")


# ---------------------------------------------------------------------------

_EXAMPLES: dict = json.loads((Path(__file__).parent / "mock_examples.json").read_text())

# Round-robin position per role, so repeated demo requests cycle through the
# library instead of always returning the same example.
_rotation: dict[str, int] = {}


def _next_example(role: str, pool: list):
    idx = _rotation.get(role, 0)
    _rotation[role] = (idx + 1) % len(pool)
    return pool[idx]


def _keyword_match(text: str, pool: list):
    """Pick the pool entry whose keywords appear in `text`; None if no match."""
    text = text.lower()
    for entry in pool:
        if any(k in text for k in entry.get("keywords", [])):
            return entry
    return None


def _mock_route(messages: list[dict]) -> str:
    """Keyword routing so every flow can be demoed without a GPU."""
    last = messages[-1]["content"].lower() if messages else ""
    if any(w in last for w in ("quiz", "question", "practice", "test me", "drill")):
        return '{"route": "quiz", "chapter": 1, "topic": "certification"}'
    if any(w in last for w in ("progress", "how am i doing", "stats", "study next")):
        return '{"route": "progress", "chapter": null, "topic": "general"}'
    if any(w in last for w in ("hello", "hi", "hey", "thanks", "what can you")):
        return '{"route": "chat", "chapter": null, "topic": "general"}'
    return '{"route": "explain", "chapter": null, "topic": "general"}'


_COACH_ONLY_FIELDS = {"bloom_level", "feedback", "trap", "next_step", "textbook_pointer"}


def _mock_coach(messages: list[dict]) -> str:
    """Match feedback to whichever question was just answered, when possible."""
    last = messages[-1]["content"] if messages else ""
    match = _keyword_match(last, _EXAMPLES["coach_feedback"])
    chosen = match or _next_example("coach", _EXAMPLES["coach_feedback"])
    return json.dumps({k: v for k, v in chosen.items() if k in _COACH_ONLY_FIELDS})


def _mock_scaffold(messages: list[dict]) -> str:
    """Socratic hint tied to whichever question the student just got wrong."""
    last = messages[-1]["content"] if messages else ""
    match = _keyword_match(last, _EXAMPLES["coach_feedback"])
    if match and match.get("scaffold_hint"):
        return match["scaffold_hint"]
    return _EXAMPLES["scaffold_fallback"]


def _mock_explain(messages: list[dict]) -> str:
    """Match a canned explanation to the student's topic; ask them to name one
    if nothing matches, instead of guessing an unrelated topic.

    Only the student's own words are matched — not the RAG companion-doc
    context bundled into the same message — so retrieved doc text can't
    accidentally trigger an unrelated example."""
    last = messages[-1]["content"] if messages else ""
    student_said = last.rsplit("The student asks:", 1)[-1]
    match = _keyword_match(student_said, _EXAMPLES["explanations"])
    if match:
        return match["text"]
    return _EXAMPLES["explain_fallback"]


async def chat(role: str, system: str, messages: list[dict]) -> str:
    """Send a chat completion request. `role` picks generation settings."""
    if config.MOCK_LLM:
        if role == "router":
            return _mock_route(messages)
        if role == "question":
            return json.dumps(_next_example("question", _EXAMPLES["questions"]))
        if role == "scaffold":
            return _mock_scaffold(messages)
        if role == "coach":
            return _mock_coach(messages)
        if role == "explain":
            return _mock_explain(messages)
        if role == "progress":
            return _next_example("progress", _EXAMPLES["progress_notes"])
        return _next_example("chat", _EXAMPLES["chat_replies"])

    gen = config.GEN.get(role, config.GEN["chat"])
    payload = {
        "model": config.LLM_MODEL,
        "messages": [{"role": "system", "content": system}] + messages,
        "temperature": gen["temperature"],
        "max_tokens": gen["max_tokens"],
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(
            f"{config.LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
