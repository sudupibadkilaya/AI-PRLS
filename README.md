# AI-PRLS — AI Study Partner for NBCOT Exam Preparation

A multi-agent tutoring system for occupational therapy students, built for the
AI-PRLS research pilot. Students chat with one tutor; behind the chat, a small
team of AI agents writes original NBCOT-style questions, coaches the student's
clinical reasoning, explains concepts Socratically, and narrates progress —
while always pointing students back to their own TherapyEd textbook instead of
reproducing it.

- **No fine-tuning.** All pedagogy lives in prompts (`backend/agents/prompts.py`)
  and retrieval over the team's own companion documents (`companion_docs/`).
- **Runs fully local** on 3x 40GB GPUs (e.g. RTX 6000 Ada class) with vLLM
  serving Qwen2.5-14B-Instruct. No student data leaves the machine.
- **Research-ready.** Every message, answer, stated reasoning, and thumbs
  rating is logged to SQLite under anonymous study IDs.

## How it works

```
Student ──► Chat UI ──► FastAPI ──► Router agent ─┬─► Question Maker ──► question card
 (browser)  (plain      (app.py)   (intent +      ├─► Reasoning Coach ─► feedback on the WHY
             academic               chapter/topic)├─► Explainer ───────► Socratic explanation
             theme)                               ├─► Progress ────────► plain-language summary
                                                  └─► Chat ────────────► greetings / study info
                                          │
                              RAG over companion_docs/   SQLite research log
                              (team's OWN notes — never  (messages, attempts,
                               the textbook's text)       reasoning, feedback)
```

All agents share one locally served LLM; they differ only in prompt, sampling
settings, and inputs. The Reasoning Coach is the heart of the design: after
every practice question the student states *why* they chose their answer, and
the coach responds to the reasoning, names the trap if one applies, and ends
with a pointer to the student's own textbook.

## Hardware plan (3x ~40GB GPUs)

| GPU | Job |
|---|---|
| 0 + 1 | vLLM serving `Qwen/Qwen2.5-14B-Instruct`, tensor parallel = 2, 16k context |
| 2 | free — use for embeddings (`AIPRLS_EMBED_DEVICE=cuda:2`), or a second model for A/B pilots |

The 14B model in bf16 uses ~28GB for weights; splitting across two cards leaves
comfortable room for KV cache and a full classroom of concurrent students.
Embeddings (bge-small, ~130MB) run fine on CPU if you'd rather keep GPU 2 free.

## Quick start

### 1. Environment

**GPU server (conda, preferred):**

```bash
conda env create -f environment.yml
conda activate aiprls
```

**Laptop / no GPUs (venv, skip vLLM):**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install "fastapi>=0.111" "uvicorn[standard]>=0.30" "httpx>=0.27" \
  "pydantic>=2.7" "numpy>=1.26" "sentence-transformers>=3.0"
```

(On a non-GPU machine with conda, delete the `vllm` line from `environment.yml`
first and use mock mode — see below.)

### 2. Build the retrieval index

```bash
python scripts/build_index.py
```

This indexes the markdown files in `companion_docs/`. Two example files are
included; replace them with the team's own chapter maps and crosswalks
(Parts I–VIII), then rerun the command. **Never put textbook text in that
folder** — see `companion_docs/README.md`.

### 3. Start the LLM (GPU machine)

```bash
bash scripts/start_llm.sh
```

First run downloads the model (~28GB) from Hugging Face. Wait for
`Uvicorn running on ... :8001`.

### 4. Start the app

```bash
python app.py
```

Open http://localhost:8000, enter a study ID, check the consent box, and talk
to the tutor:

- `quiz me on chapter 1`
- `I don't understand the difference between certification and licensure`
- `how am I doing?`

### No GPUs handy? Mock mode

```bash
AIPRLS_MOCK_LLM=1 python app.py
```

The full interface, database, and question flow work with canned model
responses — useful for frontend work and IRB demos on a laptop.

Quick check: `bash scripts/smoke_test.sh` (expects mock mode; all tests should PASS).

## Chapter sessions and instructor review

Added in response to Prof. Dumitrescu's Sep 20 feedback.

**Chapter sessions (student).** In the sidebar, pick a chapter and click
**Start 20-question session**. Each question runs the full
Attempt → Scaffold → Reconsider → Feedback → Reflect loop and shows
"Question N of 20". After each reflection the tutor replies *"Thanks for
reflecting on that — it's logged. Ready for another case whenever you are."*
and offers a **Next case** button. After question 20 (or **End session &
see summary**) the student gets summative feedback covering **only that
session**: score, how many questions were solved without a hint, reflections,
a per-question table, and a written summary of their reasoning patterns and
next steps. Sessions are stored in the `sessions` table; each attempt records
its `session_id`.

**Instructor review and coaching.** Set a shared key on the server, then
restart the app:

```bash
export AIPRLS_INSTRUCTOR_KEY='choose-a-long-random-key'   # never commit this
```

On the sign-in page, click **Instructor access** and enter the key. The
instructor can:

- see every student (study ID), with how many questions each has answered,
  their accuracy and their reflections;
- open a student to read each case, the option they chose next to the answer
  key, their stated reasoning, whether they needed a hint, and their
  reflection, plus the summary of each chapter session;
- **coach through the AI:** write guidance such as "prompt her to check
  safety before choosing an intervention". The tutor follows active notes in
  that student's questions, hints, feedback, explanations and session
  summaries. The student sees each new note once, labelled "Coaching from
  your instructor". Notes can be retired at any time.

The instructor API (`/api/instructor/*`) requires the `X-Instructor-Key`
header. If `AIPRLS_INSTRUCTOR_KEY` is unset, the instructor view is disabled.
Typing the instructor key into the Study ID box also opens the instructor view.

**Chapters (updated Oct 2 after Prof. Dumitrescu's feedback).** Chapter 1 is an
overview of the exam process, so practice questions use Chapters 2–16 only.
Students can pick a chapter in the sidebar or type a request such as
"10 questions from Chapter 14", which starts a numbered 10-question session.
Questions stay on the requested chapter: companion notes written for another
chapter (detected from each note's first heading, e.g.
`# Chapter map — TherapyEd Chapter 14: <title>`) are not used for it. The
Question Maker writes case-based items that require analysis and synthesis of
the case, not recall. Add one chapter-map note per chapter to
`companion_docs/` and run `python scripts/build_index.py` to improve each
chapter's questions. Team-only resources must not be committed to this
public repository.

## Configuration

Everything is an environment variable (defaults in `backend/config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `AIPRLS_LLM_MODEL` | `Qwen/Qwen2.5-14B-Instruct` | model vLLM serves and the app requests |
| `AIPRLS_LLM_URL` | `http://localhost:8001/v1` | OpenAI-compatible endpoint |
| `AIPRLS_EMBED_DEVICE` | `cpu` | set `cuda:2` to use the spare GPU |
| `AIPRLS_MOCK_LLM` | `0` | `1` = run without any LLM server |
| `AIPRLS_PORT` | `8000` | web app port |
| `AIPRLS_SESSION_LENGTH` | `20` | questions per chapter session |
| `AIPRLS_CHAPTERS` | `16` | chapters offered in the chapter picker |
| `AIPRLS_INSTRUCTOR_KEY` | *(empty)* | instructor sign-in key; empty disables the instructor view |

Swapping the model is one variable — the pedagogy (prompts + companion docs)
does not change, which is the point.

## Research data

SQLite file at `data/aiprls.sqlite3`:

- `students` — study IDs and consent timestamps
- `messages` — every turn in every conversation
- `attempts` — each answered question with the student's selections, their
  stated reasoning, and the verdict
- `feedback` — helpful / not-helpful ratings per tutor message
- `sessions` — chapter sessions (chapter, length, completion, summative feedback)
- `coaching_notes` — instructor guidance per student (active / retired, when seen)

Export for analysis with e.g.
`sqlite3 -header -csv data/aiprls.sqlite3 "select * from attempts;" > attempts.csv`.

## Project layout

```
app.py                     FastAPI entry; serves API + frontend
backend/
  config.py                all settings
  llm.py                   vLLM client (+ mock mode)
  rag.py                   retrieval over companion docs
  db.py                    SQLite research logging
  orchestrator.py          the Tutor Manager
  agents/
    prompts.py             ← the pedagogy lives here; edit with the team
    __init__.py            router, question maker, coach, explainer, progress
companion_docs/            the team's OWN notes (never textbook text)
frontend/                  plain academic chat UI (no build step)
scripts/
  start_llm.sh             vLLM launch for the 3-GPU box
  build_index.py           (re)build the retrieval index
data/                      SQLite DB + index (created at runtime)
```

## Responsible-use guardrails (built in)

- Every agent prompt forbids reproducing or closely paraphrasing the TherapyEd
  textbook; the tutor refers to it by chapter/topic only and directs students
  to their own copies.
- The login screen and tutor identify the system as a research study tool that
  can make mistakes and is not affiliated with NBCOT.
- All data is keyed by anonymous study IDs; run the system on the local
  network per your IRB protocol.

## License / status

Research prototype for the AI-PRLS Educational Design Research project
(Version 1, Summer 2026 pilot). Not for clinical use.
