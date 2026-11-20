"""The Tutor Manager: receives a student message, routes it, runs the right
specialist, logs everything for research, and returns a payload the frontend
can render (plain text, a question card, or a progress card)."""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid

from . import agents, book, config, db

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

# The last quiz report per student, so a report request that is retried (e.g.
# the connection dropped when the timer ran out) still gets the report.
_last_reports: dict[str, dict] = {}
_finishing: dict[str, asyncio.Event] = {}  # report being written right now

_STUDENT_FIELDS = ("format", "stem", "options", "chapter", "domain", "topic", "bloom_level")
REFLECT_ACK = "Thanks for reflecting on that — it's logged. Ready for another case whenever you are."


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


async def handle_open_chat(study_id: str, message: str, thread_id: str | None) -> dict:
    """Open chat ("like ChatGPT"): a free conversation kept apart from the tutor."""
    thread_id = thread_id or uuid.uuid4().hex
    history = db.open_thread_messages(study_id, thread_id, config.OPEN_CHAT_HISTORY)
    db.log_message(study_id, "student", message, "open", thread_id)
    try:
        text = await agents.open_chat(message, history, guidance=db.active_guidance(study_id))
    except Exception:
        return {"type": "text", "route": "open", "thread_id": thread_id, "message_id": None,
                "text": "Sorry, I couldn't reach the AI model just now. Please try again in a moment."}
    mid = db.log_message(study_id, "assistant", text, "open", thread_id)
    return {"type": "text", "text": text, "message_id": mid, "route": "open",
            "thread_id": thread_id}


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
    if sess and not _is_quiz(sess) and sess["current"] >= sess["total"]:
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


# --- quiz sessions (Dr. Dumitrescu Oct 29; Prof. Xi / team call Nov 18) -----
# A session is N questions (5, 10 or 20) from one chapter or all chapters.
#   timed    - exam style: straight to the next question, report at the end.
#   practice - feedback after each question (correct answer, why, and which
#              chapter / section to go back to), report at the end.
# Adaptive difficulty: a correct answer moves the next question up a level, a
# wrong one down a level (1 Foundation .. 3 Challenge). The next session
# starts at a level set by this session's score.
# With the e-book loaded, every question is written FROM a retrieved passage
# and records its chapter / section / pages; the report then lists exactly
# which sections to review. Questions are written ahead in the background
# (one candidate for each level the next question could be), so "Next" is fast.

QUIZ_MODES = ("timed", "practice")


def _is_quiz(sess: dict | None) -> bool:
    return bool(sess) and sess.get("mode") in QUIZ_MODES


def _clamp(level: int) -> int:
    return max(1, min(3, level))


def _quiz_info(sess: dict) -> dict:
    timed = sess["mode"] == "timed"
    left = max(0, int(round(sess["deadline"] - time.time()))) if timed else None
    return {"number": sess["current"], "total": sess["total"], "chapter": sess["chapter"],
            "mode": sess["mode"], "seconds_left": left,
            "time_limit": sess["time_limit"] if timed else None,
            "level": sess["level"], "level_name": config.LEVELS[sess["level"]]}


def _session_info(study_id: str) -> dict | None:
    s = _sessions.get(study_id)
    if not s:
        return None
    if _is_quiz(s):
        return _quiz_info(s)
    return {"number": s["current"], "total": s["total"], "chapter": s["chapter"]}


# Rotate across practice areas so a quiz is not several cases of the same kind
# (used when no e-book is loaded; with the book, the passages give variety).
PRACTICE_AREAS = [
    "neurological rehabilitation (e.g. stroke, brain injury, spinal cord injury, MS, Parkinson's)",
    "orthopedics, hand and upper extremity (e.g. fractures, tendon repair, joint replacement)",
    "mental health (e.g. schizophrenia, bipolar disorder, depression, anxiety, substance use)",
    "pediatrics (e.g. autism, cerebral palsy, developmental delay, sensory processing, school-based)",
    "older adults and cognition (e.g. dementia, low vision, fall prevention, aging in place)",
    "medical and cardiopulmonary (e.g. COPD, heart failure, cancer, burns, diabetes, amputation)",
    "work, community and wellness (e.g. ergonomics, return to work, driving, home modification)",
    "rheumatology and chronic conditions (e.g. arthritis, chronic pain, fibromyalgia)",
]


def _wanted_levels(sess: dict) -> list[int]:
    """Levels the NEXT question could be at."""
    if not config.ADAPTIVE or not sess["served"]:
        return [sess["level"]]
    if sess["answered"] < len(sess["served"]):          # current question still open
        lv = sess["level"]
        return list(dict.fromkeys([_clamp(lv + 1), _clamp(lv - 1)]))
    return [sess["level"]]


async def _write_quiz_question(study_id: str, sess: dict, level: int) -> dict:
    written = sess["served"] + sess["ready"]
    slot = len(sess["served"])                            # position of the next question
    areas = sess.setdefault("areas", random.sample(PRACTICE_AREAS, len(PRACTICE_AREAS)))
    passage = None
    if book.available():
        passage = book.pick_passage(sess["chapter"], sess["used_chunks"],
                                    min_chapter=config.FIRST_PRACTICE_CHAPTER)
        if passage:
            sess["used_chunks"].add(passage["id"])
    q = await agents.make_question(
        study_id, sess["chapter"], "general",
        avoid_topics=[x.get("topic") for x in written if x.get("topic")],
        position=(slot + 1, sess["total"]),
        guidance=db.active_guidance(study_id),
        avoid_cases=[agents.case_signature(x) for x in written],
        practice_area=None if passage else areas[slot % len(areas)],
        level=level, passage=passage,
    )
    if sess["chapter"]:
        q["chapter"] = sess["chapter"]
    q["level"] = level
    return q


async def _fill_quiz(study_id: str, sess: dict) -> None:
    """Write candidates ahead: one for each level the next question could be."""
    try:
        while sess["active"] and len(sess["served"]) < sess["total"]:
            have = {x["level"] for x in sess["ready"]}
            missing = [lv for lv in _wanted_levels(sess) if lv not in have]
            if not missing or len(sess["ready"]) >= 3:
                break
            try:
                q = await _write_quiz_question(study_id, sess, missing[0])
            except Exception:
                sess["failures"] += 1
                if sess["failures"] > 3:
                    break
                continue
            if not sess["active"]:
                break
            sess["ready"].append(q)
            sess["ready_event"].set()
    finally:
        sess["filler"] = None
        sess["ready_event"].set()


def _kick_quiz(study_id: str, sess: dict) -> None:
    if sess["filler"] is None and sess["active"] and sess["failures"] <= 3:
        sess["filler"] = asyncio.create_task(_fill_quiz(study_id, sess))


async def _take_next_quiz_question(study_id: str, sess: dict) -> dict | None:
    level = sess["level"]
    match = lambda: next((x for x in sess["ready"] if x["level"] == level), None)  # noqa: E731
    for _ in range(8):
        if match():
            break
        _kick_quiz(study_id, sess)
        if sess["filler"] is None:
            break
        sess["ready_event"].clear()
        if match():
            break
        await sess["ready_event"].wait()
    q = match()
    if q is None:
        return None
    sess["ready"].remove(q)
    sess["served"].append(q)
    sess["current"] = len(sess["served"])
    sess["shown_at"] = time.time()
    # drop candidates the next question can no longer use
    nxt = set(_wanted_levels(sess))
    sess["ready"] = [x for x in sess["ready"] if x["level"] in nxt]
    _kick_quiz(study_id, sess)  # keep writing ahead
    return q


def _quiz_question_payload(study_id: str, sess: dict, q: dict) -> dict:
    student_view = {k: q.get(k) for k in _STUDENT_FIELDS}
    mid = db.log_message(study_id, "assistant", json.dumps(student_view), "quiz")
    return {"type": "quiz_question", "question": student_view,
            "session": _quiz_info(sess), "message_id": mid, "route": "quiz"}


def _quiz_retry_payload(study_id: str, sess: dict) -> dict:
    text = ("I had trouble writing the next question just now. Your answers so far are "
            "saved — press Try again, or end the session to see your report.")
    mid = db.log_message(study_id, "assistant", text, "quiz")
    return {"type": "text", "text": text, "message_id": mid, "route": "quiz",
            "retry_next": True, "session": _quiz_info(sess)}


async def handle_session_start(study_id: str, chapter: int | None, total: int | None = None,
                               mode: str = "timed") -> dict:
    """Start a quiz session (timed or practice) and serve question 1."""
    if study_id in _sessions:
        await _finish_session(study_id, ended_early=True, quiet=True)
    _last_reports.pop(study_id, None)
    _pending.pop(study_id, None)
    mode = mode if mode in QUIZ_MODES else "timed"
    total = total or config.SESSION_LENGTH
    time_limit = total * config.QUIZ_SECONDS_PER_QUESTION if mode == "timed" else None
    level = db.get_level(study_id) if config.ADAPTIVE else 2
    sid = db.create_session(study_id, chapter, total, mode=mode, time_limit=time_limit)
    db.set_session_start_level(sid, level)
    now = time.time()
    sess = {"id": sid, "mode": mode, "chapter": chapter, "total": total, "current": 0,
            "served": [], "ready": [], "answered": 0, "correct": 0, "started_at": now,
            "deadline": now + (time_limit or 10 ** 9), "time_limit": time_limit, "shown_at": now,
            "filler": None, "ready_event": asyncio.Event(), "active": True, "failures": 0,
            "level": level, "start_level": level, "used_chunks": set()}
    _sessions[study_id] = sess
    label = f"Chapter {chapter}" if chapter else "all chapters"
    db.log_message(study_id, "assistant",
                   f"[{mode} session start] {label}, {total} questions, level {level}", "session")
    q = await _take_next_quiz_question(study_id, sess)
    if _sessions.get(study_id) is not sess:
        # A newer start replaced this one while question 1 was being written
        # (e.g. the button was clicked twice). Leave the newer session alone.
        return {"type": "superseded", "route": "quiz"}
    if q is None:
        _sessions.pop(study_id, None)
        sess["active"] = False
        db.complete_session(sid, "", ended_early=True)
        text = "I couldn't prepare the questions just now — please try again in a moment."
        mid = db.log_message(study_id, "assistant", text, "quiz")
        return {"type": "text", "text": text, "message_id": mid, "route": "quiz", "session": None}
    sess["started_at"] = time.time()  # the clock starts when question 1 is shown
    if time_limit:
        sess["deadline"] = sess["started_at"] + time_limit
    return _quiz_question_payload(study_id, sess, q)


def _record_answer(study_id: str, sess: dict, selected: list[int], seconds: float | None) -> dict:
    q = sess["served"][-1]
    took = seconds if isinstance(seconds, (int, float)) and 0 <= seconds < 86400 \
        else time.time() - sess["shown_at"]
    correct = sorted(selected) == sorted(q["correct"])
    verdict = "correct" if correct else "incorrect"
    db.log_message(study_id, "student", json.dumps({"selected": selected}), "quiz")
    db.log_attempt(study_id, q, selected, "", verdict, q.get("bloom_level"), False,
                   session_id=sess["id"], seconds=took)
    sess["answered"] += 1
    sess["correct"] += int(correct)
    before = sess["level"]
    if config.ADAPTIVE:
        sess["level"] = _clamp(before + (1 if correct else -1))
    return {"question": q, "correct": correct, "verdict": verdict,
            "level_before": before, "level_after": sess["level"]}


def _feedback_payload(study_id: str, sess: dict, res: dict, selected: list[int]) -> dict:
    """Practice mode: show the answer, why, and where to go back to in the book."""
    q = res["question"]
    src = q.get("source") or {}
    if res["level_after"] > res["level_before"]:
        level_note = f"Nice — the next question will be harder ({config.LEVELS[res['level_after']]})."
    elif res["level_after"] < res["level_before"]:
        level_note = f"The next question will be a bit easier ({config.LEVELS[res['level_after']]})."
    else:
        level_note = ""
    go_back = ""
    if not res["correct"]:
        go_back = (book.source_label(src) if src.get("kind") == "ebook"
                   else q.get("textbook_pointer", ""))
    payload = {
        "type": "quiz_feedback", "verdict": res["verdict"], "selected": selected,
        "correct_options": q["correct"], "rationales": q.get("rationales", []),
        "reasoning_principle": q.get("reasoning_principle", ""),
        "go_back": go_back, "level_note": level_note,
        "finished": sess["answered"] >= sess["total"],
        "session": _quiz_info(sess), "route": "quiz",
    }
    payload["message_id"] = db.log_message(study_id, "assistant", json.dumps(payload), "quiz")
    return payload


async def handle_quiz_answer(study_id: str, selected: list[int], seconds: float | None) -> dict:
    """Timed: record and go straight to the next question (or the report).
    Practice: record and return feedback; the student then asks for the next one."""
    sess = _sessions.get(study_id)
    if not _is_quiz(sess) or not sess["served"]:
        text = "You don't have a session running — pick a chapter and start one."
        mid = db.log_message(study_id, "assistant", text, "quiz")
        return {"type": "text", "text": text, "message_id": mid, "route": "quiz", "session": None}
    res = None
    if sess["answered"] < len(sess["served"]):  # ignore double submits
        res = _record_answer(study_id, sess, selected, seconds)
        _kick_quiz(study_id, sess)              # the next level is known now
    if sess["mode"] == "practice":
        if res is None:
            return handle_session_next_sync(study_id, sess)
        return _feedback_payload(study_id, sess, res, selected)
    if time.time() > sess["deadline"] + 5:
        return await _finish_session(study_id, ended_early=True, timed_out=True)
    if sess["answered"] >= sess["total"]:
        return await _finish_session(study_id, ended_early=False)
    q = await _take_next_quiz_question(study_id, sess)
    if q is None:
        return _quiz_retry_payload(study_id, sess)
    return _quiz_question_payload(study_id, sess, q)


def handle_session_next_sync(study_id: str, sess: dict) -> dict:
    return _quiz_question_payload(study_id, sess, sess["served"][-1])


async def handle_session_next(study_id: str) -> dict:
    """Next question (practice mode), or the current one again / a retry."""
    sess = _sessions.get(study_id)
    if not sess:
        text = "You don't have a session running — pick a chapter to start one."
        mid = db.log_message(study_id, "assistant", text, "session")
        return {"type": "text", "text": text, "message_id": mid, "route": "session"}
    if not _is_quiz(sess):
        _pending.pop(study_id, None)
        if sess["current"] >= sess["total"]:
            return await _finish_session(study_id, ended_early=False)
        return await _serve_question(study_id, sess["chapter"], "general")
    if sess["served"] and sess["answered"] < len(sess["served"]):
        return handle_session_next_sync(study_id, sess)
    if sess["answered"] >= sess["total"]:
        return await _finish_session(study_id, ended_early=False)
    sess["failures"] = 0
    q = await _take_next_quiz_question(study_id, sess)
    if q is None:
        return _quiz_retry_payload(study_id, sess)
    return _quiz_question_payload(study_id, sess, q)


async def handle_session_end(study_id: str, timed_out: bool = False) -> dict:
    """The student ends the session, or the timer ran out: report on what was answered."""
    if study_id not in _sessions:
        if study_id in _finishing:  # a retry while the first request is still writing it
            try:
                await asyncio.wait_for(_finishing[study_id].wait(), timeout=90)
            except asyncio.TimeoutError:
                pass
        if study_id in _last_reports:
            return _last_reports[study_id]
        text = "There's no session running right now."
        mid = db.log_message(study_id, "assistant", text, "session")
        return {"type": "text", "text": text, "message_id": mid, "route": "session"}
    _pending.pop(study_id, None)
    sess = _sessions[study_id]
    early = sess.get("answered", 0) < sess.get("total", 0) if _is_quiz(sess) else True
    return await _finish_session(study_id, ended_early=early, timed_out=timed_out)


def _next_session_level(sess: dict) -> int:
    """Adaptive sessions: this session's score sets the next session's level."""
    if not config.ADAPTIVE:
        return sess["start_level"]
    answered = sess["answered"]
    if answered < min(5, sess["total"]):
        return sess["start_level"]                   # too few answers to judge
    score = sess["correct"] / answered
    if score >= config.LEVEL_UP_SCORE:
        return _clamp(sess["start_level"] + 1)
    if score < config.LEVEL_DOWN_SCORE:
        return _clamp(sess["start_level"] - 1)
    return sess["start_level"]


async def _finish_session(study_id: str, ended_early: bool, timed_out: bool = False,
                          quiet: bool = False) -> dict:
    sess = _sessions.pop(study_id)
    if not _is_quiz(sess):
        return await _finish_reasoning_session(study_id, sess, ended_early)
    sess["active"] = False
    if sess.get("filler"):
        sess["filler"].cancel()
    elapsed = time.time() - sess["started_at"]
    time_used = min(elapsed, sess["time_limit"]) if sess["time_limit"] else elapsed
    next_level = _next_session_level(sess)
    db.set_level(study_id, next_level)
    if quiet:
        db.complete_session(sess["id"], "", True, time_used, next_level)
        return {}
    done = _finishing[study_id] = asyncio.Event()
    try:
        return await _write_quiz_report(study_id, sess, ended_early, timed_out, time_used, next_level)
    finally:
        done.set()
        _finishing.pop(study_id, None)


async def _write_quiz_report(study_id: str, sess: dict, ended_early: bool, timed_out: bool,
                             time_used: float, next_level: int) -> dict:
    report = db.quiz_report(sess["id"])
    report["time_used"] = round(time_used)
    report["timed_out"] = timed_out
    report["mode"] = sess["mode"]
    report["start_level"] = sess["start_level"]
    report["end_level"] = sess["level"]
    report["next_level"] = next_level if config.ADAPTIVE else None
    report["adaptive"] = config.ADAPTIVE
    report["level_names"] = config.LEVELS
    for r in report.get("review", []):
        r["label"] = book.source_label(r)
    if report["answered"] == 0:
        summary = "The session ended before any questions were answered, so there's no report yet."
    else:
        try:
            summary = await asyncio.wait_for(
                agents.quiz_summary(report, guidance=db.active_guidance(study_id)), timeout=45)
        except (Exception, asyncio.TimeoutError):
            summary = _plain_quiz_summary(report)
    db.complete_session(sess["id"], summary, ended_early, time_used, next_level)
    mid = db.log_message(study_id, "assistant", summary, "session_summary")
    payload = {"type": "quiz_report", "text": summary, "report": report,
               "ended_early": ended_early, "timed_out": timed_out,
               "message_id": mid, "route": "session_summary", "session": None}
    _last_reports[study_id] = payload
    return payload


def _plain_quiz_summary(report: dict) -> str:
    """Summary without the model (used if the model is slow or unavailable)."""
    mins = round((report.get("time_used") or 0) / 60)
    lines = [f"Overall — {report.get('correct', 0)} of {report.get('planned_questions', 0)} correct "
             f"({report.get('answered', 0)} answered) in about {mins} min."]
    review = report.get("review") or []
    if review:
        lines.append("What to review — " + "; ".join(r.get("label") or f"Chapter {r['chapter']}"
                                                       for r in review[:4]) + ".")
    lines.append("Next steps — Open each missed question below to see the correct answer and "
                 "why, then reread those sections in your TherapyEd book.")
    return "\n\n".join(lines)


async def _finish_reasoning_session(study_id: str, sess: dict, ended_early: bool) -> dict:
    """Older untimed sessions (reasoning dialogue + reflection after each case)."""
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
