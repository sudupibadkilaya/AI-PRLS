#!/usr/bin/env bash
# End-to-end smoke test in mock mode (no GPUs needed).
set -uo pipefail
cd "$(dirname "$0")/.."

rm -f data/aiprls.sqlite3
AIPRLS_MOCK_LLM=1 python app.py > /tmp/aiprls_test.log 2>&1 &
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

R=$(curl -s -X POST localhost:8000/api/chat $J -d '{"study_id":"S-TEST1","message":"quiz me on chapter 1"}')
check "quiz returns question card" '"type":"question"' "$R"
check "question is single-format only" '"format":"single"' "$R"
if echo "$R" | grep -q '"correct"'; then echo "FAIL  answer leak: correct indices sent to client"; FAIL=1; else echo "PASS  no answer leak"; fi

# Wrong first attempt (correct answer for the pacing question is index 1) -> Scaffold, no reveal.
R=$(curl -s -X POST localhost:8000/api/answer $J -d '{"study_id":"S-TEST1","selected":[0],"explanation":"re-reading every item seems most thorough"}')
check "wrong attempt returns scaffold hint" '"type":"scaffold"' "$R"
if echo "$R" | grep -q '"correct_options"'; then echo "FAIL  answer leak: scaffold step revealed the answer"; FAIL=1; else echo "PASS  scaffold does not reveal the answer"; fi

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

python3 - << 'PYEOF'
import sqlite3
con = sqlite3.connect("data/aiprls.sqlite3")
print("INFO  messages logged in DB:", con.execute("select count(*) from messages").fetchone()[0])
print("INFO  attempt row:", con.execute("select verdict, explanation, used_scaffold, reflection from attempts").fetchall())
print("INFO  feedback rows:", con.execute("select rating from feedback").fetchall())
PYEOF

if [ "$FAIL" = "0" ]; then echo "== ALL TESTS PASSED =="; else echo "== FAILURES =="; exit 1; fi
