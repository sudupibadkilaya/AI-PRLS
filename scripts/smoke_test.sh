#!/usr/bin/env bash
# End-to-end smoke test in mock mode (no GPUs needed).
set -uo pipefail
cd "$(dirname "$0")/.."

rm -f data/aiprls.sqlite3
AIPRLS_MOCK_LLM=1 AIPRLS_PORT=8000 AIPRLS_SESSION_LENGTH=3 AIPRLS_INSTRUCTOR_KEY=smoke-test-key \
  python app.py > /tmp/aiprls_test.log 2>&1 &
PID=$!
trap "kill $PID 2>/dev/null" EXIT
sleep 4

J='-H Content-Type:application/json'
FAIL=0
check () {  # name, expected_substring, actual
  if echo "$3" | grep -q "$2"; then echo "PASS  $1";
  else echo "FAIL  $1 -> $3"; FAIL=1; fi
}

R=$(curl -s -X POST localhost:8000/api/login $J -d '{"study_id":"S-TEST1","consent":true}')
check "login" '"ok":true' "$R"

R=$(curl -s -o /dev/null -w "%{http_code}" -X POST localhost:8000/api/login $J -d '{"study_id":"S-TEST1","consent":false}')
check "login refuses without consent" "400" "$R"

R=$(curl -s -X POST localhost:8000/api/chat $J -d '{"study_id":"S-TEST1","message":"hello"}')
check "chat routes small talk" '"route":"chat"' "$R"

R=$(curl -s -X POST localhost:8000/api/chat $J -d '{"study_id":"S-TEST1","message":"I dont understand splinting"}')
check "chat routes explain" '"route":"explain"' "$R"

R=$(curl -s -X POST localhost:8000/api/chat $J -d '{"study_id":"S-TEST1","message":"quiz me on chapter 3"}')
check "quiz returns question card" '"type":"question"' "$R"
check "question is single-format only" '"format":"single"' "$R"
if echo "$R" | grep -q '"correct"'; then echo "FAIL  answer leak: correct indices sent to client"; FAIL=1; else echo "PASS  no answer leak"; fi

# Wrong first attempt -> the MKO probes the reasoning first (no reveal), with
# rising support (reasoning coach -> client perspective -> domain expert).
R=$(curl -s -X POST localhost:8000/api/answer $J -d '{"study_id":"S-TEST1","selected":[0],"explanation":"re-reading every item seems most thorough"}')
check "answer opens the reasoning dialogue" '"type":"probe"' "$R"
if echo "$R" | grep -q '"correct_options"'; then echo "FAIL  answer leak: probe revealed the answer"; FAIL=1; else echo "PASS  probe does not reveal the answer"; fi
R=$(curl -s -X POST localhost:8000/api/probe $J -d '{"study_id":"S-TEST1","reply":"the case says limited time"}')
check "second probe adapts support" '"support_level":2' "$R"
R=$(curl -s -X POST localhost:8000/api/probe $J -d '{"study_id":"S-TEST1","reply":"the client wants to finish"}')
check "third probe gives knowledge support" '"agent":"expert"' "$R"
R=$(curl -s -X POST localhost:8000/api/probe $J -d '{"study_id":"S-TEST1","reply":"I would reconsider"}')
check "after the dialogue the student reconsiders" '"type":"scaffold"' "$R"
if echo "$R" | grep -q '"correct_options"'; then echo "FAIL  answer leak: reconsider step revealed the answer"; FAIL=1; else echo "PASS  reconsider does not reveal the answer"; fi

# Reconsider: second (correct) attempt -> full Feedback.
R=$(curl -s -X POST localhost:8000/api/answer $J -d '{"study_id":"S-TEST1","selected":[1],"explanation":"no penalty for guessing so use remaining time to review"}')
check "reconsidered attempt returns feedback" '"type":"feedback"' "$R"
check "feedback includes verdict" '"verdict":"correct"' "$R"
check "feedback includes reasoning principle" '"reasoning_principle"' "$R"
check "feedback includes reflection prompt" '"reflection_prompt"' "$R"

# Reflect step.
R=$(curl -s -X POST localhost:8000/api/reflect $J -d '{"study_id":"S-TEST1","reflection":"I will re-read the case for the prioritization keyword before answering."}')
check "reflection accepted" '"type":"text"' "$R"

R=$(curl -s -X POST localhost:8000/api/answer $J -d '{"study_id":"S-TEST1","selected":[0],"explanation":"x"}')
check "answer without open question handled" "quiz me" "$R"

MSGID=$(curl -s -X POST localhost:8000/api/chat $J -d '{"study_id":"S-TEST1","message":"hello"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['message_id'])")
R=$(curl -s -X POST localhost:8000/api/feedback $J -d "{\"study_id\":\"S-TEST1\",\"message_id\":\"$MSGID\",\"rating\":1}")
check "feedback logged" '"ok":true' "$R"

R=$(curl -s localhost:8000/api/progress/S-TEST1)
check "progress stats count attempt" '"total_attempts":1' "$R"
check "progress tracks reflections completed" '"reflections_completed":1' "$R"

R=$(curl -s -X POST localhost:8000/api/chat $J -d '{"study_id":"S-TEST1","message":"how am I doing?"}')
check "progress route" '"type":"progress"' "$R"

R=$(curl -s localhost:8000/ | head -20)
check "frontend served" "AI-PRLS Study Partner" "$R"

# --- Chapter session (length 3 for the test) + instructor review/coaching ---
python3 - << 'PYEOF' || FAIL=1
import json, urllib.request, urllib.error
BASE = "http://localhost:8000"
def call(path, body=None, key=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"X-Instructor-Key": key} if key else {})})
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, {}
ok = True
def check(name, cond):
    global ok
    print(("PASS  " if cond else "FAIL  ") + name); ok &= bool(cond)

S = "S-SESS1"
call("/api/login", {"study_id": S, "consent": True})
_, r = call("/api/session/start", {"study_id": S, "chapter": 2})
check("session starts with question 1 of 3", r.get("type") == "question" and r["session"] == {"number": 1, "total": 3, "chapter": 2})
check("session question has no answer leak", "correct" not in json.dumps(r.get("question", {})))
for n in (1, 2, 3):
    # answer, talk through the reasoning, then reconsider if needed
    _, a = call("/api/answer", {"study_id": S, "selected": [0], "explanation": "because"})
    while a["type"] == "probe":
        _, a = call("/api/probe", {"study_id": S, "reply": "my reasoning"})
    if a["type"] == "scaffold":
        _, a = call("/api/answer", {"study_id": S, "selected": [1], "explanation": "reconsidered"})
    check(f"question {n} feedback carries session counter", a["type"] == "feedback" and a["session"]["number"] == n)
    _, r = call("/api/reflect", {"study_id": S, "reflection": f"reflection {n}"})
    if n < 3:
        check(f"reflection {n} acknowledged with Cristina's wording",
              r.get("text") == "Thanks for reflecting on that \u2014 it's logged. Ready for another case whenever you are.")
        _, r = call("/api/session/next", {"study_id": S})
        check(f"next serves question {n+1} of 3", r.get("session", {}).get("number") == n + 1)
check("last reflection returns summative feedback", r.get("type") == "session_summary")
check("summary covers only this session", r.get("report", {}).get("total_attempts") == 3
      and r["report"].get("reflections_completed") == 3)

code, _ = call("/api/instructor/students", key="wrong")
check("instructor API rejects bad key", code == 401)
code, _ = call("/api/instructor/students")
check("instructor API rejects missing key", code == 401)
K = "smoke-test-key"
_, st = call("/api/instructor/students", key=K)
check("instructor sees students", any(x["study_id"] == S for x in st))
_, at = call(f"/api/instructor/attempts?study_id={S}", key=K)
check("instructor sees reasoning + reflections", len(at) == 3 and all(x["reflection"] and x["explanation"] for x in at))
check("instructor sees the reasoning dialogue", all(x["dialogue"] for x in at))
_, ss = call(f"/api/instructor/sessions?study_id={S}", key=K)
check("instructor sees session summary", len(ss) == 1 and ss[0]["summary"])
_, n = call("/api/instructor/coach", {"study_id": S, "note": "Ask what the client's safety needs are first."}, key=K)
check("instructor can add a coaching note", n.get("ok"))
_, r = call("/api/chat", {"study_id": S, "message": "quiz me"})
check("student receives the coaching note once", r.get("instructor_note", "").startswith("Ask what"))
_, r = call("/api/chat", {"study_id": S, "message": "hello"})
check("coaching note not repeated", "instructor_note" not in r)
raise SystemExit(0 if ok else 1)
PYEOF

python3 - << 'PYEOF'
import sqlite3
con = sqlite3.connect("data/aiprls.sqlite3")
print("INFO  messages logged in DB:", con.execute("select count(*) from messages").fetchone()[0])
print("INFO  attempt row:", con.execute("select verdict, explanation, used_scaffold, reflection from attempts").fetchall())
print("INFO  feedback rows:", con.execute("select rating from feedback").fetchall())
PYEOF

if [ "$FAIL" = "0" ]; then echo "== ALL TESTS PASSED =="; else echo "== FAILURES =="; exit 1; fi
