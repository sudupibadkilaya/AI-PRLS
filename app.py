"""AI-PRLS server: serves the chat API and the static frontend.

Run:  python app.py          (after starting the LLM with scripts/start_llm.sh)
Mock: AIPRLS_MOCK_LLM=1 python app.py   (no GPUs needed; canned responses)
"""
from __future__ import annotations

import uvicorn
from fastapi import FastAPI, HTTPException
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
    if config.MOCK_LLM:
        print("[AI-PRLS] MOCK mode — canned responses, no LLM server required.")


@app.post("/api/login")
def login(body: LoginBody):
    if not body.consent:
        raise HTTPException(400, "Consent is required to participate.")
    db.register_student(body.study_id.strip())
    return {"ok": True}


@app.post("/api/chat")
async def chat(body: ChatBody):
    return await orchestrator.handle_chat(body.study_id.strip(), body.message.strip())


@app.post("/api/answer")
async def answer(body: AnswerBody):
    return await orchestrator.handle_answer(
        body.study_id.strip(), body.selected, body.explanation.strip()
    )


@app.post("/api/reflect")
async def reflect(body: ReflectBody):
    return await orchestrator.handle_reflect(body.study_id.strip(), body.reflection.strip())


@app.post("/api/feedback")
def feedback(body: FeedbackBody):
    if body.rating not in (1, -1):
        raise HTTPException(400, "rating must be 1 or -1")
    db.log_feedback(body.study_id.strip(), body.message_id, body.rating)
    return {"ok": True}


@app.get("/api/progress/{study_id}")
def progress(study_id: str):
    return db.stats(study_id.strip())


# Static frontend (must be mounted last so /api keeps priority)
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")


if __name__ == "__main__":
    uvicorn.run(app, host=config.HOST, port=config.PORT)
