"""The Tutor Manager: receives a student message, routes it, runs the right
specialist, logs everything for research, and returns a payload the frontend
can render (plain text, a question card, or a progress card)."""
from __future__ import annotations

import json
import re
import time

from . import agents, config, db

# One pending question per student, held server-side across the
# Encounter -> articulate reasoning -> MKO probes (adaptive, fading support)
# -> reconsider -> justify -> feedback -> reflect loop.
# Shape: {"question": q, "stage": "attempt" | "probe" | "reconsider" | "reflect",
#         "first_selected": list[int] | None, "first_explanation": str | None,
#         "attempt_id": str | None}
_pending: dict[str, dict] = {}

# One active chapter practice session per student ("select a chapter,
# answer 20 questions, get summative feedback at the end").
# Shape: {"id": session_id, "chapter": int | None, "total": int,
#         "current": int  # number of the question most recently served}
_sessions: dict[str, dict] = {}

_STUDENT_FIELDS = ("format", "stem", "options", "chapter", "domain", "topic", "bloom_level")
REFLECT_ACK = "Thanks for reflecting on that — it's logged. Ready for another case whenever you are."


def _session_info(study_id: str) -> dict | None:
    s = _sessions.get(study_id)
    if not s:
        return None
    return {"number": s["current"], "total": s["total"], "chapter": s["chapter"]}


async def _serve_question(study_id: str, chapter: int | None, topic: str) -> dict:
    """Generate a question (inside the student's session if one is active),
    open it as the pending question, and return the question-card payload."""
    sess = _sessions.get(study_id)
    try:
        if sess:
            q = await agents.make_question(
                study_id, sess["chapter"], topic,
                avoid_topics=db.session_topics(sess["id"]),
                position=(sess["current"] + 1, sess["total"]),
                guidance=db.active_guidance(study_id),
            )
            if sess["chapter"]:
                q["chapter"] = sess["chapter"]
        else:
            q = await agents.make_question(study_id, chapter, topic,
                                           guidance=db.active_guidance(study_id))
    except Exception:
        text = ("I had trouble writing a well-formed question just now — "
                "please ask me again in a moment.")
        mid = db.log_message(study_id, "assistant", text, "quiz")
        return {"type": "text", "text": text, "message_id": mid, "route": "quiz",
                "retry_next": bool(sess)}
    if sess:
        sess["current"] += 1
    _pending[study_id] = {
        "question": q, "stage": "attempt",
        "first_selected": None, "first_explanation": None, "attempt_id": None,
    }
    student_view = {k: q[k] for k in _STUDENT_FIELDS}
    mid = db.log_message(study_id, "assistant", json.dumps(student_view), "quiz")
    return {
        "type": "question",
        "question": student_view,
        "instruction": "Choose the ONE best answer, then briefly tell me why you chose it.",
        "session": _session_info(study_id),
        "message_id": mid,
        "route": "quiz",
    }

REFLECTION_PROMPT = (
    "Reflect for a moment: what did you initially focus on or misunderstand, "
    "what helped you reconsider, and what principle from this case could you "
    "apply to a different client?"
)

AGENT_LABELS = {"reasoning": "Reasoning coach", "expert": "Domain expert",
                "patient": "Client's perspective"}


def _independence(study_id: str) -> str:
    """Fading: how independently the student has reasoned in recent cases
    (this session if one is running). high = strong first reasoning without
    a hint on the last 3 cases."""
    sess = _sessions.get(study_id)
    rep = db.session_report(sess["id"]) if sess else None
    recent = (rep["questions"][-3:] if rep else [])
    if len(recent) >= 3 and all(q["initial_reasoning"] == "strong" and not q["used_scaffold"] for q in recent):
        return "high"
    if recent and sum(q["initial_reasoning"] == "weak" for q in recent) >= 2:
        return "low"
    return "medium"


def _max_turns(quality: str, correct: bool, independence: str) -> int:
    """How many MKO prompts before moving on. Support fades as the student
    shows independent reasoning."""
    if correct:
        n = 1 if quality == "strong" else 2
    else:
        n = 2 if quality == "strong" else 3
    if independence == "high":
        n -= 1
    return max(1, n)


def _probe_payload(study_id: str, turn: dict, n: int, max_n: int) -> dict:
    mid = db.log_message(study_id, "assistant", turn["content"], "probe")
    return {
        "type": "probe",
        "message": turn["content"],
        "agent": turn["agent"],
        "agent_label": AGENT_LABELS.get(turn["agent"], "Tutor"),
        "support_level": turn["support_level"],
        "turn": n, "max_turns": max_n,
        "session": _session_info(study_id),
        "message_id": mid, "route": "probe",
    }


async def _next_probe(study_id: str, pending: dict) -> dict:
    q = pending["question"]
    tutor_turns = [d for d in pending["dialogue"] if d["role"] == "tutor"]
    r = await agents.probe(
        q, pending["first_selected"], pending["first_explanation"], pending["first_correct"],
        pending["dialogue"], len(tutor_turns) + 1, pending["independence"],
        guidance=db.active_guidance(study_id),
    )
    if not tutor_turns:
        pending["max_turns"] = _max_turns(r["reasoning_quality"], pending["first_correct"],
                                          pending["independence"])
    turn = {"role": "tutor", "content": r["message"], "agent": r["agent"],
            "support_level": r["support_level"], "reasoning_quality": r["reasoning_quality"],
            "gap": r["gap"], "ts": time.time()}
    pending["dialogue"].append(turn)
    pending["last_ready"] = r["ready"]
    return _probe_payload(study_id, turn, len(tutor_turns) + 1, pending["max_turns"])


async def handle_chat(study_id: str, message: str) -> dict:
    db.log_message(study_id, "student", message)
    history = db.recent_messages(study_id)

    decision = await agents.route(message, history)
    route = decision["route"]

    if route == "quiz":
        chapter, count = _parse_quiz_request(message, decision.get("chapter"))
        if chapter is not None and chapter < config.FIRST_PRACTICE_CHAPTER:
            text = (f"Chapter {chapter} is an overview of the exam process, so it isn't used "
                    f"for practice questions. Pick a chapter from {config.FIRST_PRACTICE_CHAPTER} "
                    f"to {config.CHAPTERS} — for example, \"10 questions from Chapter 14\".")
            mid = db.log_message(study_id, "assistant", text, route)
            return {"type": "text", "text": text, "message_id": mid, "route": route}
        if chapter is not None and chapter > config.CHAPTERS:
            chapter = None
        # "10 questions from chapter 14" -> a real numbered session.
        if count:
            return await handle_session_start(study_id, chapter, count)
        if study_id in _sessions:
            return await handle_session_next(study_id)
        return await _serve_question(study_id, chapter, decision.get("topic") or "general")

    if route == "progress":
        note, stats = await agents.progress_note(study_id)
        mid = db.log_message(study_id, "assistant", note, route)
        return {"type": "progress", "text": note, "stats": stats,
                "message_id": mid, "route": route}

    if route == "explain":
        text = await agents.explain(message, decision.get("topic") or message, history,
                                    guidance=db.active_guidance(study_id))
    else:
        text = await agents.small_talk(message, history)
    mid = db.log_message(study_id, "assistant", text, route)
    return {"type": "text", "text": text, "message_id": mid, "route": route}


async def handle_answer(study_id: str, selected: list[int], explanation: str) -> dict:
    pending = _pending.get(study_id)
    if not pending or pending.get("stage") not in ("attempt", "reconsider"):
        text = "I don't have an open question for you — say \"quiz me\" to get one."
        mid = db.log_message(study_id, "assistant", text, "coach")
        return {"type": "text", "text": text, "message_id": mid, "route": "coach"}

    q = pending["question"]
    db.log_message(
        study_id, "student",
        json.dumps({"selected": selected, "explanation": explanation}),
    )

    if pending["stage"] == "attempt":
        # Encounter -> articulate reasoning -> the MKO probes the thinking
        # BEFORE anything is revealed, whether or not the choice is correct.
        pending["first_selected"] = selected
        pending["first_explanation"] = explanation
        pending["first_correct"] = sorted(selected) == sorted(q["correct"])
        pending["dialogue"] = []
        pending["independence"] = _independence(study_id)
        pending["stage"] = "probe"
        return await _next_probe(study_id, pending)

    # stage == "reconsider": the second attempt, after the reasoning dialogue.
    return await _finish_with_feedback(
        study_id, pending, selected, explanation, used_scaffold=True,
        first_selected=pending["first_selected"], first_explanation=pending["first_explanation"],
    )


async def handle_probe(study_id: str, reply: str) -> dict:
    """The student answers the tutor's prompt. Probe again (adaptive support)
    or move on: correct choice -> feedback; otherwise -> reconsider."""
    pending = _pending.get(study_id)
    if not pending or pending.get("stage") != "probe":
        text = "There's no open case discussion right now — say \"quiz me\" to get a case."
        mid = db.log_message(study_id, "assistant", text, "probe")
        return {"type": "text", "text": text, "message_id": mid, "route": "probe"}
    db.log_message(study_id, "student", reply, "probe")
    pending["dialogue"].append({"role": "student", "content": reply, "ts": time.time()})
    tutor_turns = sum(1 for d in pending["dialogue"] if d["role"] == "tutor")
    if tutor_turns < pending["max_turns"] and not pending.get("last_ready"):
        return await _next_probe(study_id, pending)
    q = pending["question"]
    if pending["first_correct"]:
        return await _finish_with_feedback(
            study_id, pending, pending["first_selected"], pending["first_explanation"],
            used_scaffold=False)
    # Reasoning made visible, gaps surfaced: reconsider and choose again.
    pending["stage"] = "reconsider"
    hint = ("You've talked through your reasoning. With what you just reconsidered, "
            "look at the case again and choose the ONE best answer.")
    mid = db.log_message(study_id, "assistant", hint, "scaffold")
    return {
        "type": "scaffold", "hint": hint,
        "question": {k: q[k] for k in _STUDENT_FIELDS},
        "session": _session_info(study_id),
        "instruction": "Make and justify your decision: choose the ONE best answer and tell me why.",
        "message_id": mid, "route": "scaffold",
    }


async def _finish_with_feedback(
    study_id: str, pending: dict, selected: list[int], explanation: str,
    used_scaffold: bool, first_selected: list[int] | None = None,
    first_explanation: str | None = None,
) -> dict:
    q = pending["question"]
    dialogue = pending.get("dialogue") or []
    result = await agents.coach(q, selected, explanation, first_selected, first_explanation,
                                guidance=db.active_guidance(study_id), dialogue=dialogue)
    sess = _sessions.get(study_id)
    attempt_id = db.log_attempt(
        study_id, q, selected, explanation, result["verdict"],
        result.get("bloom_level"), used_scaffold,
        session_id=sess["id"] if sess else None, dialogue=dialogue,
    )
    result["correct_options"] = q["correct"]
    result["rationales"] = q.get("rationales", [])
    result["reasoning_principle"] = q.get("reasoning_principle", "")
    result["reflection_prompt"] = REFLECTION_PROMPT
    result["session"] = _session_info(study_id)

    pending["stage"] = "reflect"
    pending["attempt_id"] = attempt_id

    mid = db.log_message(study_id, "assistant", json.dumps(result), "coach")
    return {"type": "feedback", **result, "message_id": mid, "route": "coach"}


async def handle_reflect(study_id: str, reflection: str) -> dict:
    pending = _pending.get(study_id)
    if not pending or pending.get("stage") != "reflect" or not pending.get("attempt_id"):
        text = "There's nothing waiting on a reflection right now — say \"quiz me\" to start a new case."
        mid = db.log_message(study_id, "assistant", text, "reflect")
        return {"type": "text", "text": text, "message_id": mid, "route": "reflect"}

    db.log_message(study_id, "student", reflection, "reflect")
    db.update_reflection(pending["attempt_id"], reflection)
    _pending.pop(study_id, None)

    mid = db.log_message(study_id, "assistant", REFLECT_ACK, "reflect")
    sess = _sessions.get(study_id)
    if sess and sess["current"] >= sess["total"]:
        # Last question of the session: acknowledge, then summative feedback.
        summary = await _finish_session(study_id, ended_early=False)
        return {**summary, "ack": REFLECT_ACK, "ack_message_id": mid}
    return {"type": "text", "text": REFLECT_ACK, "message_id": mid, "route": "reflect",
            "session": _session_info(study_id)}


# --- chapter practice sessions ------------------------------------------------

_COUNT_RE = re.compile(r"\b(\d{1,2})\s*(?:\w+\s+){0,2}?(?:questions?|qs|cases?|items?|mcqs?)\b", re.I)
_CHAPTER_RE = re.compile(r"\b(?:chapter|ch\.?)\s*(\d{1,2})\b", re.I)


def _parse_quiz_request(message: str, routed_chapter) -> tuple[int | None, int | None]:
    """Read "10 questions from chapter 14" directly from the student's words
    (more reliable than the router for numbers). Returns (chapter, count)."""
    m = _CHAPTER_RE.search(message)
    chapter = int(m.group(1)) if m else (routed_chapter if isinstance(routed_chapter, int) else None)
    count = None
    for m in _COUNT_RE.finditer(message):
        # don't mistake "chapter 14 questions" for 14 questions
        before = message[:m.start()].rstrip().lower()
        if before.endswith(("chapter", "ch", "ch.")):
            continue
        count = max(1, min(int(m.group(1)), config.MAX_SESSION_LENGTH))
        break
    return chapter, count


async def handle_session_start(study_id: str, chapter: int | None, total: int | None = None) -> dict:
    """Start a chapter session and serve question 1."""
    if study_id in _sessions:
        # Starting over abandons the old session (it stays in the DB, marked early).
        old = _sessions.pop(study_id)
        db.complete_session(old["id"], "", ended_early=True)
    _pending.pop(study_id, None)
    total = total or config.SESSION_LENGTH
    sid = db.create_session(study_id, chapter, total)
    _sessions[study_id] = {"id": sid, "chapter": chapter, "total": total, "current": 0}
    label = f"Chapter {chapter}" if chapter else "mixed chapters"
    db.log_message(study_id, "assistant",
                   f"[session start] {label}, {total} questions", "session")
    return await _serve_question(study_id, chapter, "general")


async def handle_session_next(study_id: str) -> dict:
    """Serve the next question of the active session (or the summary if done)."""
    sess = _sessions.get(study_id)
    if not sess:
        text = "You don't have a chapter session running — pick a chapter to start one."
        mid = db.log_message(study_id, "assistant", text, "session")
        return {"type": "text", "text": text, "message_id": mid, "route": "session"}
    pending = _pending.get(study_id)
    if pending and pending.get("stage") in ("attempt", "probe", "reconsider"):
        text = "Let's finish the current case first — choose an answer and tell me why."
        mid = db.log_message(study_id, "assistant", text, "session")
        return {"type": "text", "text": text, "message_id": mid, "route": "session"}
    # A pending "reflect" stage means the student skipped the reflection.
    _pending.pop(study_id, None)
    if sess["current"] >= sess["total"]:
        return await _finish_session(study_id, ended_early=False)
    return await _serve_question(study_id, sess["chapter"], "general")


async def handle_session_end(study_id: str) -> dict:
    """Student stops early: summarize whatever was answered so far."""
    if study_id not in _sessions:
        text = "There's no chapter session running right now."
        mid = db.log_message(study_id, "assistant", text, "session")
        return {"type": "text", "text": text, "message_id": mid, "route": "session"}
    _pending.pop(study_id, None)
    return await _finish_session(study_id, ended_early=True)


async def _finish_session(study_id: str, ended_early: bool) -> dict:
    sess = _sessions.pop(study_id)
    report = db.session_report(sess["id"])
    if report["total_attempts"] == 0:
        summary = "You ended the session before answering any questions, so there's nothing to summarize yet."
    else:
        try:
            summary = await agents.session_summary(report, guidance=db.active_guidance(study_id))
        except Exception:
            summary = (f"Session complete: {report['total_correct']} of "
                       f"{report['total_attempts']} correct.")
    db.complete_session(sess["id"], summary, ended_early)
    mid = db.log_message(study_id, "assistant", summary, "session_summary")
    return {
        "type": "session_summary",
        "text": summary,
        "report": report,
        "ended_early": ended_early,
        "message_id": mid,
        "route": "session_summary",
    }


def attach_instructor_note(study_id: str, payload: dict) -> dict:
    """The first time a new instructor note is in effect, show it to the student
    too, so the coaching is transparent rather than hidden inside the AI."""
    note = db.take_undelivered_note(study_id)
    if note:
        payload["instructor_note"] = note
        db.log_message(study_id, "assistant", f"[instructor note shown] {note}", "instructor")
    return payload
