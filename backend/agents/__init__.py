"""The agent team. Each function is one specialist; the orchestrator composes them."""
from __future__ import annotations

import json
import re

from .. import config, db, llm, rag
from . import prompts


def _guidance_block(guidance: list[str] | None) -> str:
    """Instructor coaching notes, passed to the AI so it can coach 'through' them."""
    if not guidance:
        return ""
    notes = "\n".join(f"- {g}" for g in guidance)
    return (
        "\n\nINSTRUCTOR GUIDANCE — the student's faculty instructor reviewed their "
        "work and asked you to coach them this way. Follow it where it is relevant "
        "to this case; you may say \"your instructor suggested...\". It never "
        "overrides the answer key or the rules above.\n" + notes + "\n"
    )


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


async def make_question(
    study_id: str,
    chapter: int | None,
    topic: str,
    avoid_topics: list[str] | None = None,
    position: tuple[int, int] | None = None,
    guidance: list[str] | None = None,
) -> dict:
    """Generate one original NBCOT-style item, grounded in companion notes.

    avoid_topics: topics already used (e.g. earlier in this chapter session).
    position: (n, total) when the item is part of a chapter session, so the
    maker can vary domains and difficulty across the set."""
    query = f"chapter {chapter} {topic}" if chapter else topic
    context = rag.context_block(query, chapter=chapter)
    recent = avoid_topics if avoid_topics is not None else db.recent_topics(study_id)
    if chapter:
        title = rag.chapter_title(chapter)
        chapter_line = f"Chapter {chapter}" + (f" ({title})" if title else "")
        if not rag.has_chapter_notes(chapter):
            chapter_line += (" — the team has no chapter notes for it yet, so base the case "
                             "on the entry-level OT practice content this chapter covers")
    else:
        chapter_line = (f"any chapter from {config.FIRST_PRACTICE_CHAPTER} to "
                        f"{config.CHAPTERS} (never Chapter 1)")
    session_note = ""
    if position:
        n, total = position
        session_note = (
            f"This is question {n} of {total} in a practice session on this chapter. "
            "Cover a different scenario and sub-topic from the earlier questions, "
            "and vary the NBCOT domain across the session.\n"
        )
    user_msg = (
        f"Requested chapter: {chapter_line}\n"
        f"Requested topic: {topic}\n{session_note}\n"
        f"Companion notes (the study team's own material):\n{context}\n\n"
        f"Topics of this student's recent questions (avoid repeating these "
        f"scenarios): {', '.join(recent) if recent else 'none yet'}\n\n"
        "Write one new case-based, single-best-answer question now."
        + _guidance_block(guidance)
    )
    # Generate, check against the team's item-writing rules, and ask the model
    # to revise when a rule is broken (it is a mid-sized model and does not
    # always follow every rule on the first try).
    messages = [{"role": "user", "content": user_msg}]
    best: tuple[int, dict] | None = None
    last_error: Exception | None = None
    for _ in range(4):
        raw = await llm.chat("question", prompts.QUESTION_MAKER, messages)
        try:
            q = llm.extract_json(raw)
            _validate_question(q)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            messages = messages[:1] + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"That was not usable ({exc}). Output the JSON item again, following every rule."},
            ]
            continue
        issues = [] if config.MOCK_LLM else quality_issues(q)
        if best is None or len(issues) < best[0]:
            best = (len(issues), q)
        if not issues:
            break
        messages = messages[:1] + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": "Revise this item. It breaks these item-writing rules:\n- "
             + "\n- ".join(issues) + "\nOutput only the corrected JSON item."},
        ]
    if best is None:
        raise ValueError(f"No valid question after retries: {last_error}")
    q = best[1]
    q.pop("_key_conflict", None)
    if chapter:
        q["chapter"] = chapter  # the requested chapter is authoritative
    return q


_PT_RE = re.compile(r"\b(gait|ambulat\w*|stairs?|walking|walker training|quadriceps|hamstrings?|"
                    r"strengthening exercises?|strengthen(ing)? the (leg|knee|hip|thigh)|range[- ]of[- ]motion "
                    r"exercises?|manual therapy|joint mobili[sz]ation|therapeutic ultrasound|transfers? training)\b")
_OCC_RE = re.compile(r"\b(adl|iadl|dress\w*|bath\w*|shower\w*|toilet\w*|groom\w*|feed\w*|eat\w*|cook\w*|"
                     r"meal|kitchen|laundry|home|household|work\w*|job|school|leisure|play|occupation\w*|"
                     r"daily (activities|living|tasks|routine)|routine|adaptive|assistive|equipment|environment\w*|"
                     r"modif\w*|compensatory|energy conservation|caregiver|task|activity)\b")
_KEYWORD_RE = re.compile(r"\b(FIRST|NEXT|MOST|BEST|PRIMARY|PRIORITY|CONTRAINDICATED)\b")
_NEGATIVE_RE = re.compile(r"\b(EXCEPT|NOT|LEAST)\b")
_CUE_RE = re.compile(r"\b(may|might|could)\b", re.I)
_NAME_RE = re.compile(r"\b(Mr|Mrs|Ms|Miss|Dr)\.?\s+[A-Z]|\bnamed\s+[A-Z]")
_RECALL_RE = re.compile(r"^(what is the (definition|meaning|name|underlying cause|cause)|which of the following (is|defines|describes)|what does .* stand for)", re.I)


def quality_issues(q: dict) -> list[str]:
    """Check an item against the study team's MCQ writing standards."""
    issues = []
    stem = q.get("stem", "").strip()
    sentences = [x for x in re.split(r"(?<=[.?!])\s+", stem) if x]
    question = sentences[-1] if sentences else ""
    if not question.endswith("?"):
        issues.append("The stem must end with a single question (ending in '?').")
    if not _KEYWORD_RE.search(question):
        issues.append("The final question must contain a key word in CAPITALS: FIRST, NEXT, "
                      "MOST effective, MOST important, BEST, PRIMARY or CONTRAINDICATED.")
    if "most appropriate" in stem.lower():
        issues.append("Do not use 'most appropriate'; use MOST effective, MOST important, FIRST or NEXT.")
    if _NEGATIVE_RE.search(question):
        issues.append("Do not use a negative stem (NOT, EXCEPT, LEAST).")
    if _CUE_RE.search(stem):
        issues.append("Remove cue words (may, might, could) from the stem.")
    if _NAME_RE.search(stem):
        issues.append("Do not name the client; write 'the client' in third person.")
    if _RECALL_RE.search(question):
        issues.append("The question asks for recall or a cause. Ask for a therapist decision or action "
                      "(what to do FIRST/NEXT, the MOST effective intervention, the MOST important "
                      "information), and make every option an action the therapist could take.")
    if not 3 <= len(sentences) <= 6:
        issues.append("The stem must be 3-5 sentences: setting/population/diagnosis, then the "
                      "problem, then the question.")
    opts = [o.strip() for o in q.get("options", [])]
    lowered = [o.lower() for o in opts]
    if any(k in o for o in lowered for k in ("all of the above", "none of the above", "both ")):
        issues.append("No 'all/none of the above' or 'both' options.")
    if any(re.search(r"\b(always|never)\b", o) for o in lowered):
        issues.append("Remove absolute words (always, never) from the options.")
    lengths = [len(o) for o in opts if o]
    if lengths and max(lengths) > 2.5 * min(lengths):
        issues.append("Make all four options similar in length and tone.")
    if len(set(lowered)) < len(lowered):
        issues.append("All four options must be different.")
    key_text = opts[q["correct"][0]].lower() if opts and q.get("correct") else ""
    if _PT_RE.search(key_text) and not _OCC_RE.search(key_text):
        issues.append("The correct answer is a physical therapy intervention. Make the correct answer an "
                      "occupation-based OT action (adapting the task or environment, adaptive equipment, "
                      "ADL/IADL training in a meaningful occupation, compensatory strategies, caregiver "
                      "training) and center the case on occupational performance.")
    if _PT_RE.search(stem.lower()) and not _OCC_RE.search(stem.lower()):
        issues.append("Center the case on an occupational performance problem (self-care, home, work, "
                      "school, leisure), not on walking, stairs or strength alone.")
    if q.get("_key_conflict"):
        issues.append("The answer key and the rationales disagree about which option is correct. "
                      "Make 'correct' and the rationales consistent, with rationales in the same "
                      "order as the options.")
    return issues


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
        q["bloom_level"] = "analysis"
    if q["bloom_level"] in ("knowledge", "comprehension") and not config.MOCK_LLM:
        raise ValueError("Recall-level item; NBCOT practice needs case reasoning")
    if not q.get("reasoning_principle"):
        q["reasoning_principle"] = "Prioritize the option that best matches this client's current stage and safety needs."
    _align_rationales(q)


_LABEL_RE = re.compile(
    r"^\s*(?:option\s*)?(?:[A-D]\s*[\).:\-\u2014]\s*)?(?:\u2014\s*)?"
    r"(?P<label>correct|incorrect|right|wrong|best answer|not the best( choice)?)\s*[:.\-\u2014]\s*",
    re.I)
_STOP = {"the", "and", "for", "with", "this", "that", "client", "clients", "client's", "therapist",
         "would", "should", "because", "their", "from", "into", "they", "them", "more", "most",
         "than", "will", "have", "been", "before", "after", "about", "which", "while"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 3 and w not in _STOP}


def _align_rationales(q: dict) -> None:
    """The model sometimes lists rationales in a different order than the
    options. Match each rationale to the option it discusses, drop the model's
    own Correct/Incorrect labels, and relabel from the real answer key."""
    import itertools
    opts, rats = q["options"], list(q.get("rationales", []))
    if len(rats) != len(opts):
        return
    labels, texts = [], []
    for r in rats:
        m = _LABEL_RE.match(r or "")
        lab = (m.group("label").lower() if m else "")
        labels.append(lab in ("correct", "right", "best answer"))
        texts.append(r[m.end():].strip() if m else (r or "").strip())
    ow, rw = [_words(o) for o in opts], [_words(t) for t in texts]
    score = lambda i, j: len(ow[i] & rw[j])
    ident = sum(score(i, i) for i in range(len(opts)))
    best = max(itertools.permutations(range(len(opts))),
               key=lambda p: sum(score(i, p[i]) for i in range(len(opts))))
    if sum(score(i, best[i]) for i in range(len(opts))) > ident:
        texts = [texts[best[i]] for i in range(len(opts))]
        labels = [labels[best[i]] for i in range(len(opts))]
    key = q["correct"][0]
    # The rationales argue for a different option than the answer key.
    q["_key_conflict"] = any(labels) and not labels[key]
    q["rationales"] = [("Correct \u2014 " if i == key else "Not the best choice \u2014 ") + t
                       for i, t in enumerate(texts)]


async def scaffold(question: dict, selected: list[int], explanation: str,
                   guidance: list[str] | None = None) -> str:
    """Socratic hint after a wrong first attempt. Never reveals the answer."""
    user_msg = (
        f"QUESTION JSON (for your reference only — do not reveal the correct "
        f"answer):\n{json.dumps(question, indent=2)}\n\n"
        f"STUDENT'S WRONG SELECTION (zero-based index): {selected}\n"
        f"STUDENT'S EXPLANATION OF WHY: {explanation or '(none given)'}\n\n"
        "Give the Socratic hint now."
        + _guidance_block(guidance)
    )
    return await llm.chat("scaffold", prompts.SCAFFOLD, [{"role": "user", "content": user_msg}])


async def coach(
    question: dict,
    selected: list[int],
    explanation: str,
    first_selected: list[int] | None = None,
    first_explanation: str | None = None,
    guidance: list[str] | None = None,
    dialogue: list[dict] | None = None,
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
    if dialogue:
        user_msg += "REASONING DIALOGUE (before the answer was revealed):\n" + _format_dialogue(dialogue) + "\n\n"
    user_msg += (
        f"STUDENT'S FINAL SELECTION (zero-based index): {selected}\n"
        f"STUDENT'S FINAL EXPLANATION OF WHY: {explanation or '(none given)'}\n\n"
        f"Scoring computed by the system: {verdict_hint}. Use this verdict.\n"
        "Give full feedback now."
        + _guidance_block(guidance)
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


async def explain(message: str, topic: str, history: list[dict],
                  guidance: list[str] | None = None) -> str:
    context = rag.context_block(topic)
    user_msg = (
        f"Companion notes (the study team's own material):\n{context}\n"
        f"{_guidance_block(guidance)}\n"
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


async def session_summary(report: dict, guidance: list[str] | None = None) -> str:
    """Summative feedback for one chapter practice session."""
    # Keep the payload compact: trim long free-text fields.
    compact = dict(report)
    compact["questions"] = [
        {**q, "explanation": q["explanation"][:300], "reflection": q["reflection"][:300]}
        for q in report.get("questions", [])
    ]
    return await llm.chat(
        "summary",
        prompts.SESSION_SUMMARY,
        [{"role": "user", "content": f"Session report JSON:\n{json.dumps(compact, indent=2)}"
          + _guidance_block(guidance)}],
    )


async def small_talk(message: str, history: list[dict]) -> str:
    convo = history[-6:] + [{"role": "user", "content": message}]
    return await llm.chat("chat", prompts.CHAT, convo)


def _format_dialogue(dialogue: list[dict]) -> str:
    return "\n".join(
        f"{'TUTOR (' + d.get('agent', 'reasoning') + ')' if d['role'] == 'tutor' else 'STUDENT'}: {d['content']}"
        for d in dialogue
    )


_AGENTS = {"reasoning", "expert", "patient"}
_QUALITY = {"strong", "partial", "weak"}


async def probe(question: dict, selected: list[int], explanation: str, is_correct: bool,
                dialogue: list[dict], turn: int, independence: str,
                guidance: list[str] | None = None) -> dict:
    """One MKO turn: judge the student's reasoning and ask one adaptive prompt.
    Never reveals the answer."""
    user_msg = (
        f"QUESTION JSON (for your reference only — never reveal the key):\n"
        f"{json.dumps(question, indent=2)}\n\n"
        f"STUDENT'S CHOICE (zero-based index): {selected} — system scoring: "
        f"{'correct' if is_correct else 'not the best answer'} (do NOT tell the student)\n"
        f"STUDENT'S STATED REASONING: {explanation or '(none given)'}\n\n"
        f"DIALOGUE SO FAR:\n{_format_dialogue(dialogue) if dialogue else '(none yet)'}\n\n"
        f"Turn number: {turn}. Student's recent independence level: {independence}.\n"
        "Write your next prompt now."
        + _guidance_block(guidance)
    )
    raw = await llm.chat("probe", prompts.MKO_PROBE, [{"role": "user", "content": user_msg}])
    try:
        r = llm.extract_json(raw)
    except (ValueError, json.JSONDecodeError):
        r = {"message": raw.strip()}
    msg = (r.get("message") or "").strip() or (
        "Walk me through how you arrived at that choice — what in the case mattered most?")
    try:
        level = max(0, min(3, int(r.get("support_level", 1))))
    except (TypeError, ValueError):
        level = 1
    return {
        "reasoning_quality": r.get("reasoning_quality") if r.get("reasoning_quality") in _QUALITY else "partial",
        "gap": r.get("gap") or None,
        "support_level": level,
        "agent": r.get("agent") if r.get("agent") in _AGENTS else "reasoning",
        "message": msg,
        "ready": bool(r.get("ready")),
    }
