"""The agent team. Each function is one specialist; the orchestrator composes them."""
from __future__ import annotations

import json

from .. import db, llm, rag
from . import prompts


async def route(message: str, history: list[dict]) -> dict:
    """Classify the student's message. Falls back to 'chat' on any parse issue."""
    convo = history[-4:] + [{"role": "user", "content": message}]
    raw = await llm.chat("router", prompts.ROUTER, convo)
    try:
        result = llm.extract_json(raw)
        if result.get("route") in {"quiz", "explain", "progress", "chat"}:
            return result
    except (ValueError, json.JSONDecodeError):
        pass
    return {"route": "chat", "chapter": None, "topic": "general"}


async def make_question(study_id: str, chapter: int | None, topic: str) -> dict:
    """Generate one original NBCOT-style item, grounded in companion notes."""
    query = f"chapter {chapter} {topic}" if chapter else topic
    context = rag.context_block(query)
    recent = db.recent_topics(study_id)
    user_msg = (
        f"Requested chapter: {chapter or 'any'}\n"
        f"Requested topic: {topic}\n\n"
        f"Companion notes (the study team's own material):\n{context}\n\n"
        f"Topics of this student's recent questions (avoid repeating these "
        f"scenarios): {', '.join(recent) if recent else 'none yet'}\n\n"
        "Write one new case-based, single-best-answer question now."
    )
    raw = await llm.chat("question", prompts.QUESTION_MAKER, [{"role": "user", "content": user_msg}])
    q = llm.extract_json(raw)
    _validate_question(q)
    return q


_BLOOM_LEVELS = {"knowledge", "comprehension", "application", "analysis", "synthesis", "evaluation"}


def _validate_question(q: dict) -> None:
    n_opts = len(q.get("options", []))
    n_correct = len(q.get("correct", []))
    if q.get("format") != "single" or n_opts != 4 or n_correct != 1:
        raise ValueError(f"Malformed question from model: format={q.get('format')}, "
                         f"options={n_opts}, correct={n_correct}")
    if len(q.get("rationales", [])) != n_opts:
        q["rationales"] = ["(no rationale provided)"] * n_opts
    if q.get("bloom_level") not in _BLOOM_LEVELS:
        q["bloom_level"] = "application"
    if not q.get("reasoning_principle"):
        q["reasoning_principle"] = "Prioritize the option that best matches this client's current stage and safety needs."


async def scaffold(question: dict, selected: list[int], explanation: str) -> str:
    """Socratic hint after a wrong first attempt. Never reveals the answer."""
    user_msg = (
        f"QUESTION JSON (for your reference only — do not reveal the correct "
        f"answer):\n{json.dumps(question, indent=2)}\n\n"
        f"STUDENT'S WRONG SELECTION (zero-based index): {selected}\n"
        f"STUDENT'S EXPLANATION OF WHY: {explanation or '(none given)'}\n\n"
        "Give the Socratic hint now."
    )
    return await llm.chat("scaffold", prompts.SCAFFOLD, [{"role": "user", "content": user_msg}])


async def coach(
    question: dict,
    selected: list[int],
    explanation: str,
    first_selected: list[int] | None = None,
    first_explanation: str | None = None,
) -> dict:
    """Give full feedback on the student's FINAL answer, and above all their
    reasoning. If a scaffolded first attempt is passed, the feedback
    acknowledges how the student's thinking changed between attempts."""
    correct = sorted(question["correct"])
    verdict_hint = "correct" if sorted(selected) == correct else "incorrect"
    user_msg = (
        f"QUESTION JSON:\n{json.dumps(question, indent=2)}\n\n"
    )
    if first_selected is not None:
        user_msg += (
            f"STUDENT'S FIRST (SCAFFOLDED) ATTEMPT — selected (zero-based index): "
            f"{first_selected}, explanation: {first_explanation or '(none given)'}\n\n"
        )
    user_msg += (
        f"STUDENT'S FINAL SELECTION (zero-based index): {selected}\n"
        f"STUDENT'S FINAL EXPLANATION OF WHY: {explanation or '(none given)'}\n\n"
        f"Scoring computed by the system: {verdict_hint}. Use this verdict.\n"
        "Give full feedback now."
    )
    raw = await llm.chat("coach", prompts.REASONING_COACH, [{"role": "user", "content": user_msg}])
    try:
        result = llm.extract_json(raw)
        result["verdict"] = verdict_hint  # system scoring is authoritative
        if result.get("bloom_level") not in _BLOOM_LEVELS:
            result["bloom_level"] = None
        return result
    except (ValueError, json.JSONDecodeError):
        return {
            "verdict": verdict_hint,
            "bloom_level": None,
            "feedback": raw,
            "trap": None,
            "next_step": "Try another question when you're ready.",
            "textbook_pointer": question.get("textbook_pointer", ""),
        }


async def explain(message: str, topic: str, history: list[dict]) -> str:
    context = rag.context_block(topic)
    user_msg = (
        f"Companion notes (the study team's own material):\n{context}\n\n"
        f"The student asks: {message}"
    )
    convo = history[-4:] + [{"role": "user", "content": user_msg}]
    return await llm.chat("explain", prompts.EXPLAINER, convo)


async def progress_note(study_id: str) -> tuple[str, dict]:
    s = db.stats(study_id)
    if s["total_attempts"] == 0:
        return (
            "You haven't answered any practice questions yet, so there's nothing to "
            "chart. Try \"quiz me\" to get started — your progress will build here.",
            s,
        )
    raw = await llm.chat(
        "progress",
        prompts.PROGRESS,
        [{"role": "user", "content": f"Student statistics JSON:\n{json.dumps(s, indent=2)}"}],
    )
    return raw, s


async def small_talk(message: str, history: list[dict]) -> str:
    convo = history[-6:] + [{"role": "user", "content": message}]
    return await llm.chat("chat", prompts.CHAT, convo)
