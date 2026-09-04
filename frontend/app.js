/* AI-PRLS frontend logic — no frameworks, no build step. */

const $ = (sel) => document.querySelector(sel);
let studyId = null;

/* ---------- login ---------- */
$("#login-btn").addEventListener("click", async () => {
  const id = $("#study-id").value.trim();
  const consent = $("#consent").checked;
  const err = $("#login-error");
  err.hidden = true;
  if (id.length < 3) { err.textContent = "Please enter your study ID."; err.hidden = false; return; }
  if (!consent) { err.textContent = "Please confirm consent to continue."; err.hidden = false; return; }
  const res = await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ study_id: id, consent }),
  });
  if (!res.ok) { err.textContent = "Could not sign in — please try again."; err.hidden = false; return; }
  studyId = id;
  $("#login-screen").hidden = true;
  $("#app").hidden = false;
  $("#student-label").textContent = "Signed in as " + id;
  addTutorText(
    "Welcome. I'm your study partner for NBCOT preparation. You can ask me to " +
    "quiz you (\u201cquiz me on chapter 1\u201d), explain a concept, or show your " +
    "progress. I'll often ask why you chose an answer \u2014 explaining your " +
    "reasoning is where the learning happens. For full details on any topic, " +
    "keep your TherapyEd book nearby."
  );
  $("#input").focus();
});

$("#logout-btn").addEventListener("click", () => location.reload());

/* ---------- sidebar shortcuts ---------- */
document.querySelectorAll(".nav-btn").forEach((b) =>
  b.addEventListener("click", () => sendMessage(b.dataset.prompt))
);

/* ---------- composer ---------- */
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("#input").value.trim();
  if (text) { $("#input").value = ""; sendMessage(text); }
});
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#composer").requestSubmit();
  }
});
$("#send-btn").before(dictationButton($("#input")));

/* ---------- dictation (speech-to-text) ----------
   Typed and spoken input are both first-class here: the mic button is an
   alternative to typing, not a replacement — students pick whichever fits
   the moment, especially for reasoning/reflection answers. */
function dictationButton(target) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return document.createTextNode(""); // unsupported browser: no button, no error

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "mic-btn";
  btn.title = "Dictate your answer";
  btn.textContent = "\u{1F3A4}";

  let recognition = null;
  let listening = false;

  btn.addEventListener("click", () => {
    if (listening) { recognition.stop(); return; }
    recognition = new SR();
    recognition.lang = "en-US";
    recognition.interimResults = false;
    recognition.continuous = false;
    recognition.onstart = () => { listening = true; btn.classList.add("listening"); };
    recognition.onend = () => { listening = false; btn.classList.remove("listening"); };
    recognition.onerror = () => { listening = false; btn.classList.remove("listening"); };
    recognition.onresult = (e) => {
      const heard = e.results[0][0].transcript;
      target.value = (target.value.trim() ? target.value.trim() + " " : "") + heard;
      target.dispatchEvent(new Event("input"));
      target.focus();
    };
    recognition.start();
  });

  return btn;
}

/* ---------- message rendering ---------- */
function msgShell(who, cls) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + cls;
  wrap.innerHTML = `<div class="who">${who}</div>`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  wrap.appendChild(bubble);
  $("#messages").appendChild(wrap);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return bubble;
}

function addStudent(text) {
  msgShell("You", "student").textContent = text;
}
function addTutorText(text, messageId) {
  const b = msgShell("Tutor", "tutor");
  b.textContent = text;
  if (messageId) b.parentElement.appendChild(feedbackBar(messageId));
  scrollDown();
}
function addThinking() {
  const b = msgShell("Tutor", "tutor");
  b.classList.add("thinking");
  b.textContent = "Thinking\u2026";
  return b.parentElement;
}
function scrollDown() { $("#messages").scrollTop = $("#messages").scrollHeight; }

function feedbackBar(messageId) {
  const div = document.createElement("div");
  div.className = "fb";
  const mk = (label, rating) => {
    const btn = document.createElement("button");
    btn.textContent = label;
    btn.addEventListener("click", async () => {
      await fetch("/api/feedback", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ study_id: studyId, message_id: messageId, rating }),
      });
      div.querySelectorAll("button").forEach((x) => x.classList.remove("picked"));
      btn.classList.add("picked");
    });
    return btn;
  };
  div.appendChild(mk("Helpful", 1));
  div.appendChild(mk("Not helpful", -1));
  return div;
}

/* ---------- chat flow ---------- */
async function sendMessage(text) {
  addStudent(text);
  const pending = addThinking();
  setBusy(true);
  try {
    const res = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ study_id: studyId, message: text }),
    });
    const data = await res.json();
    pending.remove();
    renderResponse(data);
  } catch {
    pending.remove();
    addTutorText("Something went wrong reaching the server. Please try again.");
  } finally {
    setBusy(false);
  }
}

function setBusy(v) { $("#send-btn").disabled = v; }

function renderResponse(data) {
  if (data.type === "question") return renderQuestion(data);
  if (data.type === "scaffold") return renderScaffold(data);
  if (data.type === "feedback") return renderFeedback(data);
  if (data.type === "progress") return renderProgress(data);
  addTutorText(data.text, data.message_id);
}

/* ---------- question card ---------- */
const DOMAINS = {1: "Evaluation & assessment", 2: "Analysis & planning",
                 3: "Interventions", 4: "Competency & practice management"};
const BLOOM_LABELS = {knowledge: "Knowledge", comprehension: "Comprehension",
                       application: "Application", analysis: "Analysis",
                       synthesis: "Synthesis", evaluation: "Evaluation"};

function renderQuestion(data, hintText) {
  const q = data.question;
  const b = msgShell("Tutor", "tutor");
  b.style.whiteSpace = "normal";

  if (hintText) {
    const hint = document.createElement("div");
    hint.className = "socratic";
    hint.innerHTML = `<div class="socratic-label">Hint \u2014 have another look</div>`;
    const hp = document.createElement("div");
    hp.textContent = hintText;
    hint.appendChild(hp);
    b.appendChild(hint);
  }

  const card = document.createElement("div");
  card.className = "qcard";

  const meta = [];
  if (q.chapter) meta.push("Chapter " + q.chapter);
  if (q.domain) meta.push("Domain " + q.domain + " \u00b7 " + (DOMAINS[q.domain] || ""));
  if (q.bloom_level) meta.push(BLOOM_LABELS[q.bloom_level] || q.bloom_level);
  meta.push("Single best answer");
  card.innerHTML = `<div class="qmeta">${meta.join("  \u00b7  ")}</div>
                    <div class="qstem"></div>
                    <div class="qinstr">${data.instruction}</div>`;
  card.querySelector(".qstem").textContent = q.stem;

  q.options.forEach((opt, i) => {
    const label = document.createElement("label");
    label.className = "qopt";
    const input = document.createElement("input");
    input.type = "radio"; input.name = "qopt"; input.value = i;
    const span = document.createElement("span");
    span.textContent = String.fromCharCode(65 + i) + ". " + opt;
    label.appendChild(input); label.appendChild(span);
    card.appendChild(label);
  });

  const why = document.createElement("div");
  why.className = "qwhy";
  why.innerHTML = `<label>Why did you choose that? (a sentence or two)</label>`;
  const ta = document.createElement("textarea");
  why.appendChild(ta);
  why.appendChild(dictationButton(ta));
  card.appendChild(why);

  const submit = document.createElement("button");
  submit.className = "qsubmit";
  submit.textContent = hintText ? "Submit answer (2nd attempt)" : "Submit answer";
  submit.addEventListener("click", () => submitAnswer(card, q, ta, submit));
  card.appendChild(submit);

  b.appendChild(card);
  scrollDown();
}

async function submitAnswer(card, q, ta, submit) {
  const selected = [...card.querySelectorAll("input[name=qopt]:checked")]
    .map((x) => parseInt(x.value, 10));
  if (selected.length !== 1) { alert("Please select one answer."); return; }

  submit.disabled = true;
  card.querySelectorAll("input").forEach((x) => (x.disabled = true));
  ta.disabled = true;

  const pending = addThinking();
  setBusy(true);
  try {
    const res = await fetch("/api/answer", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ study_id: studyId, selected, explanation: ta.value.trim() }),
    });
    const data = await res.json();
    pending.remove();
    if (data.type === "feedback") markOptions(card, selected, data.correct_options);
    renderResponse(data);
  } catch {
    pending.remove();
    addTutorText("Something went wrong scoring that answer. Please try again.");
  } finally {
    setBusy(false);
  }
}

/* ---------- scaffold (wrong first attempt) ---------- */
function renderScaffold(data) {
  renderQuestion(data, data.hint);
}

function markOptions(card, selected, correct) {
  card.querySelectorAll(".qopt").forEach((label, i) => {
    if (correct.includes(i)) label.classList.add("right");
    else if (selected.includes(i)) label.classList.add("wrongpick");
  });
}

function renderFeedback(data) {
  const b = msgShell("Tutor", "tutor");
  b.style.whiteSpace = "normal";
  const v = document.createElement("span");
  v.className = "verdict" + (data.verdict === "incorrect" ? " incorrect" : "");
  v.textContent = data.verdict === "correct" ? "Correct"
                : data.verdict === "partly" ? "Partly correct" : "Not quite";
  b.appendChild(v);

  const fb = document.createElement("div");
  fb.style.whiteSpace = "pre-wrap";
  fb.textContent = data.feedback;
  b.appendChild(fb);

  if (data.bloom_level) {
    const lvl = document.createElement("div");
    lvl.className = "bloom-tag";
    lvl.textContent = "Reasoning shown: " + (BLOOM_LABELS[data.bloom_level] || data.bloom_level);
    b.appendChild(lvl);
  }

  if (data.trap) {
    const t = document.createElement("div");
    t.className = "trap";
    t.textContent = "Reasoning trap to watch: " + data.trap;
    b.appendChild(t);
  }
  if (data.rationales && data.rationales.length) {
    const ul = document.createElement("ul");
    ul.className = "rationale-list";
    data.rationales.forEach((r, i) => {
      const li = document.createElement("li");
      li.textContent = String.fromCharCode(65 + i) + " \u2014 " + r;
      ul.appendChild(li);
    });
    b.appendChild(ul);
  }
  if (data.next_step) {
    const socratic = document.createElement("div");
    socratic.className = "socratic";
    socratic.innerHTML = `<div class="socratic-label">Think about this next</div>`;
    const q = document.createElement("div");
    q.textContent = data.next_step;
    socratic.appendChild(q);
    b.appendChild(socratic);
  }
  if (data.reasoning_principle) {
    const rp = document.createElement("div");
    rp.className = "principle";
    rp.innerHTML = `<div class="socratic-label">Reasoning principle</div>`;
    const rpText = document.createElement("div");
    rpText.textContent = data.reasoning_principle;
    rp.appendChild(rpText);
    b.appendChild(rp);
  }
  if (data.textbook_pointer) {
    const next = document.createElement("div");
    next.className = "pointer";
    next.textContent = data.textbook_pointer;
    b.appendChild(next);
  }

  b.parentElement.appendChild(feedbackBar(data.message_id));

  if (data.reflection_prompt) renderReflectPrompt(data.reflection_prompt);
  scrollDown();
}

/* ---------- reflect step ---------- */
function renderReflectPrompt(promptText) {
  const b = msgShell("Tutor", "tutor");
  b.style.whiteSpace = "normal";
  b.classList.add("reflect-card");

  const label = document.createElement("div");
  label.className = "socratic-label";
  label.textContent = "Reflect";
  b.appendChild(label);

  const q = document.createElement("div");
  q.textContent = promptText;
  b.appendChild(q);

  const ta = document.createElement("textarea");
  b.appendChild(ta);
  b.appendChild(dictationButton(ta));

  const submit = document.createElement("button");
  submit.className = "qsubmit";
  submit.textContent = "Submit reflection";
  submit.addEventListener("click", async () => {
    const text = ta.value.trim();
    if (!text) { alert("Add a sentence or two before submitting."); return; }
    submit.disabled = true; ta.disabled = true;
    try {
      const res = await fetch("/api/reflect", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ study_id: studyId, reflection: text }),
      });
      const data = await res.json();
      renderResponse(data);
    } catch {
      addTutorText("Something went wrong saving that reflection. Please try again.");
    }
  });
  b.appendChild(submit);
  scrollDown();
}

/* ---------- progress card ---------- */
function renderProgress(data) {
  const b = msgShell("Tutor", "tutor");
  b.style.whiteSpace = "normal";

  const note = document.createElement("div");
  note.style.whiteSpace = "pre-wrap";
  note.textContent = data.text;
  b.appendChild(note);

  const s = data.stats;
  if (s && s.total_attempts > 0) {
    const stats = document.createElement("div");
    stats.className = "pstats";
    stats.innerHTML = `
      <div class="pstat"><div class="num">${s.total_attempts}</div><div class="lab">questions answered</div></div>
      <div class="pstat"><div class="num">${Math.round((s.accuracy || 0) * 100)}%</div><div class="lab">overall accuracy</div></div>
      <div class="pstat"><div class="num">${s.recent10_accuracy != null ? Math.round(s.recent10_accuracy * 100) + "%" : "\u2013"}</div><div class="lab">last 10</div></div>
      <div class="pstat"><div class="num">${s.independent_rate != null ? Math.round(s.independent_rate * 100) + "%" : "\u2013"}</div><div class="lab">solved independently</div></div>
      <div class="pstat"><div class="num">${s.reflections_completed || 0}</div><div class="lab">reflections completed</div></div>`;
    b.appendChild(stats);

    const bars = document.createElement("div");
    Object.entries(s.by_domain).forEach(([d, v]) => {
      const pct = v.attempts ? Math.round((v.correct / v.attempts) * 100) : 0;
      const row = document.createElement("div");
      row.className = "pbar-row";
      row.innerHTML = `<span class="lbl">Domain ${d}</span>
                       <div class="pbar"><div style="width:${pct}%"></div></div>
                       <span class="pct">${pct}% \u00b7 n=${v.attempts}</span>`;
      bars.appendChild(row);
    });
    b.appendChild(bars);

    if (s.by_bloom_level && Object.keys(s.by_bloom_level).length) {
      const bloomHeading = document.createElement("div");
      bloomHeading.className = "pstats-heading";
      bloomHeading.textContent = "By reasoning level";
      b.appendChild(bloomHeading);

      const bloomBars = document.createElement("div");
      Object.entries(s.by_bloom_level).forEach(([lvl, v]) => {
        const pct = v.attempts ? Math.round((v.correct / v.attempts) * 100) : 0;
        const row = document.createElement("div");
        row.className = "pbar-row";
        row.innerHTML = `<span class="lbl">${BLOOM_LABELS[lvl] || lvl}</span>
                         <div class="pbar"><div style="width:${pct}%"></div></div>
                         <span class="pct">${pct}% \u00b7 n=${v.attempts}</span>`;
        bloomBars.appendChild(row);
      });
      b.appendChild(bloomBars);
    }
  }
  b.parentElement.appendChild(feedbackBar(data.message_id));
  scrollDown();
}
