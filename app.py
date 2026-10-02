"""AI-PRLS server: serves the chat API and the static frontend.

Run:  python app.py          (after starting the LLM with scripts/start_llm.sh)
Mock: AIPRLS_MOCK_LLM=1 python app.py   (no GPUs needed; canned responses)
"""
from __future__ import annotations

import secrets

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import config, db, orchestrator, rag

app = FastAPI(title="AI-PRLS", docs_url=None, redoc_url=None)


class LoginBody(BaseModel):
    study_id: str = Field(min_length=3, max_length=40)
    consent: bool


class ChatBody(BaseModel):
    study_id: str
    message: str = Field(min_length=1, max_length=4000)


class AnswerBody(BaseModel):
    study_id: str
    selected: list[int]
    explanation: str = ""


class ReflectBody(BaseModel):
    study_id: str
    reflection: str = Field(min_length=1, max_length=2000)


class SessionStartBody(BaseModel):
    study_id: str
    chapter: int | None = Field(default=None, ge=config.FIRST_PRACTICE_CHAPTER, le=config.CHAPTERS)
    total: int | None = Field(default=None, ge=1, le=config.MAX_SESSION_LENGTH)


class SessionBody(BaseModel):
    study_id: str


class FeedbackBody(BaseModel):
    study_id: str
    message_id: str
    rating: int  # 1 or -1


@app.on_event("startup")
def _startup() -> None:
    db.init()
    if not rag.load_index():
        print("[AI-PRLS] No companion-doc index found. "
              "Run: python scripts/build_index.py")
    if not config.INSTRUCTOR_KEY:
        print("[AI-PRLS] Instructor view disabled — set AIPRLS_INSTRUCTOR_KEY to enable it.")
    if config.MOCK_LLM:
        print("[AI-PRLS] MOCK mode — canned responses, no LLM server required.")


@app.post("/api/login")
def login(body: LoginBody):
    sid = body.study_id.strip()
    # An instructor who types the instructor key into the Study ID box is
    # sent to the instructor view instead of being registered as a student.
    if config.INSTRUCTOR_KEY and secrets.compare_digest(sid.encode(), config.INSTRUCTOR_KEY.encode()):
        return {"ok": True, "instructor": True}
    if not body.consent:
        raise HTTPException(400, "Consent is required to participate.")
    db.register_student(sid)
    return {"ok": True}


@app.post("/api/chat")
async def chat(body: ChatBody):
    sid = body.study_id.strip()
    return orchestrator.attach_instructor_note(
        sid, await orchestrator.handle_chat(sid, body.message.strip())
    )


@app.post("/api/answer")
async def answer(body: AnswerBody):
    sid = body.study_id.strip()
    return orchestrator.attach_instructor_note(
        sid, await orchestrator.handle_answer(sid, body.selected, body.explanation.strip())
    )


@app.post("/api/reflect")
async def reflect(body: ReflectBody):
    return await orchestrator.handle_reflect(body.study_id.strip(), body.reflection.strip())


@app.post("/api/session/start")
async def session_start(body: SessionStartBody):
    sid = body.study_id.strip()
    return orchestrator.attach_instructor_note(
        sid, await orchestrator.handle_session_start(sid, body.chapter, body.total)
    )


@app.post("/api/session/next")
async def session_next(body: SessionBody):
    sid = body.study_id.strip()
    return orchestrator.attach_instructor_note(sid, await orchestrator.handle_session_next(sid))


@app.post("/api/session/end")
async def session_end(body: SessionBody):
    return await orchestrator.handle_session_end(body.study_id.strip())


@app.get("/api/config")
def public_config():
    return {"session_length": config.SESSION_LENGTH, "chapters": config.CHAPTERS,
            "first_chapter": config.FIRST_PRACTICE_CHAPTER}


@app.post("/api/feedback")
def feedback(body: FeedbackBody):
    if body.rating not in (1, -1):
        raise HTTPException(400, "rating must be 1 or -1")
    db.log_feedback(body.study_id.strip(), body.message_id, body.rating)
    return {"ok": True}


@app.get("/api/progress/{study_id}")
def progress(study_id: str):
    return db.stats(study_id.strip())


# --- Instructor review & coaching (protected by AIPRLS_INSTRUCTOR_KEY) ---------

def require_instructor(x_instructor_key: str = Header(default="")) -> None:
    if not config.INSTRUCTOR_KEY:
        raise HTTPException(503, "Instructor access is not configured on this server.")
    if not secrets.compare_digest(x_instructor_key.encode(), config.INSTRUCTOR_KEY.encode()):
        raise HTTPException(401, "Invalid instructor key.")


class CoachBody(BaseModel):
    study_id: str
    note: str = Field(min_length=1, max_length=2000)
    attempt_id: str | None = None


class NoteToggleBody(BaseModel):
    active: bool


@app.get("/api/instructor/check", dependencies=[Depends(require_instructor)])
def instructor_check():
    return {"ok": True}


@app.get("/api/instructor/students", dependencies=[Depends(require_instructor)])
def instructor_students():
    return db.instructor_students()


@app.get("/api/instructor/attempts", dependencies=[Depends(require_instructor)])
def instructor_attempts(study_id: str):
    return db.instructor_attempts(study_id.strip())


@app.get("/api/instructor/sessions", dependencies=[Depends(require_instructor)])
def instructor_sessions(study_id: str):
    return db.instructor_sessions(study_id.strip())


@app.get("/api/instructor/coach", dependencies=[Depends(require_instructor)])
def instructor_notes(study_id: str):
    return db.list_coaching_notes(study_id.strip())


@app.post("/api/instructor/coach", dependencies=[Depends(require_instructor)])
def instructor_add_note(body: CoachBody):
    nid = db.add_coaching_note(body.study_id.strip(), body.note.strip(), body.attempt_id)
    return {"ok": True, "id": nid}


@app.post("/api/instructor/coach/{note_id}", dependencies=[Depends(require_instructor)])
def instructor_toggle_note(note_id: str, body: NoteToggleBody):
    db.set_coaching_note_active(note_id, body.active)
    return {"ok": True}


# Static frontend (must be mounted last so /api keeps priority)
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")


if __name__ == "__main__":
    uvicorn.run(app, host=config.HOST, port=config.PORT)
