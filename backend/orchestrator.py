"""The Tutor Manager: receives a student message, routes it, runs the right
specialist, logs everything for research, and returns a payload the frontend
can render (plain text, a question card, or a progress card)."""
from __future__ import annotations

import json

from . import agents, db

# One pending question per student, held server-side across the
# Attempt -> Scaffold -> Reconsider -> Respond -> Feedback -> Reflect loop.
# Shape: {"question": q, "stage": "attempt" | "reconsider" | "reflect",
#         "first_selected": list[int] | None, "first_explanation": str | None,
#         "attempt_id": str | None}
_pending: dict[str, dict] = {}

REFLECTION_PROMPT = (
    "In a sentence or two: what's the one thing you'll take from this case "
    "into the next one?"
)


async def handle_chat(study_id: str, message: str) -> dict:
    db.log_message(study_id, "student", message)
    history = db.recent_messages(study_id)

    decision = await agents.route(message, history)
    route = decision["route"]

    if route == "quiz":
        try:
            q = await agents.make_question(
                study_id, decision.get("chapter"), decision.get("topic") or "general"
            )
        except Exception:
            text = ("I had trouble writing a well-formed question just now — "
                    "please ask me again in a moment.")
            mid = db.log_message(study_id, "assistant", text, route)
            return {"type": "text", "text": text, "message_id": mid, "route": route}
        _pending[study_id] = {
            "question": q, "stage": "attempt",
            "first_selected": None, "first_explanation": None, "attempt_id": None,
        }
        student_view = {k: q[k] for k in
                        ("format", "stem", "options", "chapter", "domain", "topic", "bloom_level")}
        mid = db.log_message(study_id, "assistant", json.dumps(student_view), route)
        instruction = "Choose the ONE best answer, then briefly tell me why you chose it."
        return {
            "type": "question",
            "question": student_view,
            "instruction": instruction,
            "message_id": mid,
            "route": route,
        }

    if route == "progress":
        note, stats = await agents.progress_note(study_id)
        mid = db.log_message(study_id, "assistant", note, route)
        return {"type": "progress", "text": note, "stats": stats,
                "message_id": mid, "route": route}

    if route == "explain":
        text = await agents.explain(message, decision.get("topic") or message, history)
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
        correct = sorted(selected) == sorted(q["correct"])
        if not correct:
            # Wrong first attempt: Scaffold, then let them Reconsider — no
            # answer reveal yet.
            hint = await agents.scaffold(q, selected, explanation)
            pending["stage"] = "reconsider"
            pending["first_selected"] = selected
            pending["first_explanation"] = explanation
            mid = db.log_message(study_id, "assistant", hint, "scaffold")
            return {
                "type": "scaffold", "hint": hint,
                "question": {k: q[k] for k in
                             ("format", "stem", "options", "chapter", "domain", "topic", "bloom_level")},
                "instruction": "Reconsider, then choose the ONE best answer and tell me why.",
                "message_id": mid, "route": "scaffold",
            }
        # Correct on the first attempt: go straight to Feedback.
        return await _finish_with_feedback(study_id, pending, selected, explanation, used_scaffold=False)

    # stage == "reconsider": this is the second attempt, after a scaffold hint.
    return await _finish_with_feedback(
        study_id, pending, selected, explanation, used_scaffold=True,
        first_selected=pending["first_selected"], first_explanation=pending["first_explanation"],
    )


async def _finish_with_feedback(
    study_id: str, pending: dict, selected: list[int], explanation: str,
    used_scaffold: bool, first_selected: list[int] | None = None,
    first_explanation: str | None = None,
) -> dict:
    q = pending["question"]
    result = await agents.coach(q, selected, explanation, first_selected, first_explanation)
    attempt_id = db.log_attempt(
        study_id, q, selected, explanation, result["verdict"],
        result.get("bloom_level"), used_scaffold,
    )
    result["correct_options"] = q["correct"]
    result["rationales"] = q.get("rationales", [])
    result["reasoning_principle"] = q.get("reasoning_principle", "")
    result["reflection_prompt"] = REFLECTION_PROMPT

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

    text = "Thanks for reflecting on that — it's logged. Ready for another case whenever you are."
    mid = db.log_message(study_id, "assistant", text, "reflect")
    return {"type": "text", "text": text, "message_id": mid, "route": "reflect"}
