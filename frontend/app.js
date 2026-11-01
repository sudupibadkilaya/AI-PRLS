/* AI-PRLS frontend logic — no frameworks, no build step. */

const $ = (sel) => document.querySelector(sel);
let studyId = null;
let openMode = false;     // open chat ("like ChatGPT") vs the structured tutor
let openThread = null;    // current open-chat conversation id (server-assigned)
const pane = () => (openMode ? $("#open-messages") : $("#messages"));

/* ---------- login ---------- */
$("#login-btn").addEventListener("click", async () => {
  const id = $("#study-id").value.trim();
  const consent = $("#consent").checked;
  const err = $("#login-error");
  err.hidden = true;
  if (id.length < 3) { err.textContent = "Please enter your study ID."; err.hidden = false; return; }
  const res = await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ study_id: id, consent }),
  });
  if (!res.ok) {
    err.textContent = !consent ? "Please confirm consent to continue."
                               : "Could not sign in — please try again.";
    err.hidden = false; return;
  }
  const info = await res.json();
  if (info.instructor && window.openInstructorDashboard) {
    // The instructor key was typed into the Study ID box: go to the instructor view.
    $("#study-id").value = "";
    return window.openInstructorDashboard(id);
  }
  studyId = id;
  $("#login-screen").hidden = true;
  $("#app").hidden = false;
  $("#student-label").textContent = "Signed in as " + id;
  addTutorText(
    "Welcome. I'm your study partner for NBCOT preparation. Pick a chapter and start " +
    "a timed quiz to practice like the real exam \u2014 you'll get a performance report " +
    "at the end. You can also practice a single question with feedback on your " +
    "reasoning, ask about a concept, or check your progress. For full details on " +
    "any topic, keep your TherapyEd book nearby."
  );
  $("#input").focus();
});

$("#logout-btn").addEventListener("click", () => location.reload());

/* ---------- chapter sessions (select a chapter, N questions, summary) ---------- */
let sessionLength = 20;
let quizSecondsPerQ = 85;
fetch("/api/config").then((r) => r.json()).then((c) => {
  sessionLength = c.session_length;
  const sel = $("#chapter-select");
  for (let i = c.first_chapter || 1; i <= c.chapters; i++) {
    const o = document.createElement("option");
    const wk = (c.weeks || {})[String(i)];
    o.value = i; o.textContent = "Chapter " + i + (wk ? ` (Week ${wk})` : "");
    sel.appendChild(o);
  }
  quizSecondsPerQ = c.quiz_seconds_per_question || 85;
  const mins = Math.round((sessionLength * quizSecondsPerQ) / 60);
  $("#session-start-btn").textContent = `Start timed ${sessionLength}-question quiz`;
  $("#quiz-note").textContent = `${mins} minutes, timed like the exam. Your score report comes at the end.`;
}).catch(() => {});

$("#session-start-btn").addEventListener("click", () => {
  setOpenMode(false);
  const chapter = parseInt($("#chapter-select").value, 10) || null;
  addStudent(`Start a timed ${sessionLength}-question quiz on Chapter ${chapter}.`);
  callApi("/api/session/start", { study_id: studyId, chapter }, "Preparing your quiz\u2026");
});
$("#session-end-btn").addEventListener("click", () => {
  if (!confirm("End the quiz now and see your report?")) return;
  endQuiz(false);
});

function updateBanner(session) {
  const banner = $("#session-banner");
  if (!session) { banner.hidden = true; stopTimer(); return; }
  banner.hidden = false;
  if (session.mode === "timed" && session.seconds_left != null) startTimer(session.seconds_left);
  $("#session-label").textContent =
    `Chapter ${session.chapter ?? "–"} · Question ${session.number} of ${session.total}`;
  $("#session-fill").style.width = Math.round((session.number / session.total) * 100) + "%";
}

/* ---------- quiz timer ---------- */
let timerId = null, timerEnd = 0, quizEnding = false;
function fmtClock(sec) {
  sec = Math.max(0, Math.round(sec));
  return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0");
}
function startTimer(secondsLeft) {
  timerEnd = Date.now() + secondsLeft * 1000;
  quizEnding = false;
  $("#session-timer").hidden = false;
  if (timerId) clearInterval(timerId);
  const tick = () => {
    const left = (timerEnd - Date.now()) / 1000;
    const t = $("#session-timer");
    t.textContent = "Time left " + fmtClock(left);
    t.classList.toggle("low", left <= 120);
    if (left <= 0) { stopTimer(); endQuiz(true); }
  };
  tick();
  timerId = setInterval(tick, 1000);
}
function stopTimer() {
  if (timerId) clearInterval(timerId);
  timerId = null;
  $("#session-timer").hidden = true;
}
function endQuiz(timedOut) {
  if (quizEnding) return;
  quizEnding = true;
  if (timedOut) addTutorText("Time is up. Here is your report.");
  callApi("/api/session/end", { study_id: studyId, timed_out: !!timedOut }, "Writing your report\u2026");
}

async function callApi(url, body, waitText) {
  const pending = addThinking(waitText);
  setBusy(true);
  try {
    const res = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
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

/* ---------- phone menu ---------- */
$("#menu-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  $("#app").classList.toggle("menu-open");
});
document.addEventListener("click", (e) => {
  const app = $("#app");
  if (!app.classList.contains("menu-open")) return;
  // close after choosing something, or when tapping outside the menu
  if (!$("#sidebar").contains(e.target) || e.target.closest("button")) app.classList.remove("menu-open");
});

/* ---------- sidebar shortcuts ---------- */
document.querySelectorAll(".nav-btn[data-prompt]").forEach((b) =>
  b.addEventListener("click", () => { setOpenMode(false); sendMessage(b.dataset.prompt); })
);

/* ---------- open chat ("like ChatGPT") ----------
   A free conversation kept separate from the tutor: its own message list and
   conversation thread, so follow-up questions keep their context. */
const TUTOR_PLACEHOLDER = $("#input").placeholder;
function setOpenMode(on) {
  openMode = on;
  $("#chat-pane").classList.toggle("open-mode", on);
  $("#open-chat-btn").classList.toggle("active", on);
  $("#input").placeholder = on
    ? "Ask anything — e.g. \u201cWhat's the difference between remediation and compensation?\u201d"
    : TUTOR_PLACEHOLDER;
  if (on && !$("#open-messages").children.length) openWelcome();
  $("#input").focus();
}
function openWelcome() {
  addTutorText(
    "This is an open chat. Ask me anything about OT practice or the NBCOT exam, " +
    "in your own words, and ask follow-up questions as we go. For practice " +
    "questions and reasoning feedback, use the tutor (Back to tutor)."
  );
}
$("#open-chat-btn").addEventListener("click", () => setOpenMode(true));
$("#open-back-btn").addEventListener("click", () => setOpenMode(false));
$("#open-new-btn").addEventListener("click", () => {
  openThread = null;
  $("#open-messages").textContent = "";
  openWelcome();
  $("#input").focus();
});

async function sendOpen(text) {
  addStudent(text);
  const pending = addThinking();
  setBusy(true);
  try {
    const res = await fetch("/api/open_chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ study_id: studyId, message: text, thread_id: openThread }),
    });
    const data = await res.json();
    pending.remove();
    if (!res.ok) throw new Error();
    openThread = data.thread_id || openThread;
    addTutorText(data.text, data.message_id, true);
  } catch {
    pending.remove();
    addTutorText("Something went wrong reaching the server. Please try again.");
  } finally {
    setBusy(false);
  }
}

/* Light, safe formatting for open-chat replies: paragraphs, "- " / "1." lists
   and **bold**. Text is escaped first, so no HTML from the model is rendered. */
function renderLightMarkdown(text) {
  const esc = (t) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  let html = "", list = null;
  const close = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const raw of String(text || "").split("\n")) {
    const line = raw.trim();
    const hd = line.match(/^#{1,6}\s+(.*)/);
    if (hd) { close(); html += `<p class="md-h">${inline(hd[1])}</p>`; continue; }
    const ul = line.match(/^[-*\u2022]\s+(.*)/), ol = line.match(/^\d+[.)]\s+(.*)/);
    if (ul || ol) {
      const kind = ul ? "ul" : "ol";
      if (list !== kind) { close(); html += `<${kind}>`; list = kind; }
      html += `<li>${inline((ul || ol)[1])}</li>`;
    } else if (!line) {
      close();
    } else {
      close();
      html += `<p>${inline(line)}</p>`;
    }
  }
  close();
  return html;
}

/* ---------- composer ---------- */
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("#input").value.trim();
  if (text) { $("#input").value = ""; openMode ? sendOpen(text) : sendMessage(text); }
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
  pane().appendChild(wrap);
  pane().scrollTop = pane().scrollHeight;
  return bubble;
}

function addStudent(text) {
  msgShell("You", "student").textContent = text;
}
function addTutorText(text, messageId, formatted) {
  const b = msgShell("Tutor", "tutor");
  if (formatted) { b.classList.add("md"); b.innerHTML = renderLightMarkdown(text); }
  else b.textContent = text;
  if (messageId) b.parentElement.appendChild(feedbackBar(messageId));
  scrollDown();
}
function addThinking(text) {
  const b = msgShell("Tutor", "tutor");
  b.classList.add("thinking");
  b.textContent = text || "Thinking\u2026";
  return b.parentElement;
}
function scrollDown() { pane().scrollTop = pane().scrollHeight; }

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
  if (data.instructor_note) renderInstructorNote(data.instructor_note);
  if ("session" in data) updateBanner(data.session);
  if (data.type === "question") return renderQuestion(data);
  if (data.type === "scaffold") return renderScaffold(data);
  if (data.type === "probe") return renderProbe(data);
  if (data.type === "feedback") return renderFeedback(data);
  if (data.type === "progress") return renderProgress(data);
  if (data.type === "session_summary") return renderSessionSummary(data);
  if (data.type === "quiz_question") return renderQuizQuestion(data);
  if (data.type === "quiz_report") return renderQuizReport(data);
  addTutorText(data.text, data.message_id);
  // After a reflection inside a session: offer the next case.
  if (data.session && data.route === "reflect") renderNextButton(data.session);
  if (data.retry_next) renderNextButton(null, "Try again");
}

function renderNextButton(session, label) {
  const b = msgShell("Tutor", "tutor");
  b.style.whiteSpace = "normal";
  const row = document.createElement("div");
  row.className = "next-row";
  const btn = document.createElement("button");
  btn.className = "qsubmit";
  btn.style.marginTop = "0";
  btn.textContent = label || `Next case → Question ${session.number + 1} of ${session.total}`;
  btn.addEventListener("click", () => {
    btn.disabled = true;
    callApi("/api/session/next", { study_id: studyId });
  });
  row.appendChild(btn);
  b.appendChild(row);
  scrollDown();
}

function renderInstructorNote(note) {
  const b = msgShell("Tutor", "tutor");
  b.style.whiteSpace = "normal";
  const box = document.createElement("div");
  box.className = "instructor-note";
  box.innerHTML = `<div class="socratic-label">Coaching from your instructor</div>`;
  const t = document.createElement("div");
  t.textContent = note;
  box.appendChild(t);
  b.appendChild(box);
}

/* ---------- end-of-session summary ---------- */
function renderSessionSummary(data) {
  if (data.ack) addTutorText(data.ack, data.ack_message_id);
  updateBanner(null);
  const r = data.report || {};
  const b = msgShell("Tutor", "tutor");
  b.style.whiteSpace = "normal";
  b.classList.add("summary-card");

  const h = document.createElement("h3");
  h.textContent = (data.ended_early ? "Session ended early — summary" : "Session complete — summary")
    + (r.chapter ? ` (Chapter ${r.chapter})` : "");
  b.appendChild(h);

  if (r.total_attempts) {
    const stats = document.createElement("div");
    stats.className = "pstats";
    const pct = (x) => (x != null ? Math.round(x * 100) + "%" : "–");
    stats.innerHTML = `
      <div class="pstat"><div class="num">${r.total_correct}/${r.total_attempts}</div><div class="lab">correct</div></div>
      <div class="pstat"><div class="num">${pct(r.accuracy)}</div><div class="lab">accuracy</div></div>
      <div class="pstat"><div class="num">${pct(r.independent_rate)}</div><div class="lab">solved without a hint</div></div>
      <div class="pstat"><div class="num">${r.scaffolded_then_correct || 0}</div><div class="lab">correct after a hint</div></div>
      <div class="pstat"><div class="num">${r.reflections_completed || 0}</div><div class="lab">reflections</div></div>`;
    b.appendChild(stats);
  }

  const t = document.createElement("div");
  t.className = "summary-text";
  t.textContent = data.text;
  b.appendChild(t);

  if (r.questions && r.questions.length) {
    const head = document.createElement("div");
    head.className = "pstats-heading";
    head.textContent = "Question by question";
    b.appendChild(head);
    const table = document.createElement("table");
    table.className = "qlist";
    table.innerHTML = "<tr><th>#</th><th>Topic</th><th>Result</th><th>Hint used</th><th>Reasoning level</th></tr>";
    r.questions.forEach((q) => {
      const tr = document.createElement("tr");
      const cells = [q.number, q.topic, q.verdict === "correct" ? "Correct" : "Not quite",
                     q.used_scaffold ? "Yes" : "No", BLOOM_LABELS[q.bloom_level] || q.bloom_level || "–"];
      cells.forEach((c, i) => {
        const td = document.createElement("td");
        td.textContent = c;
        if (i === 2) td.className = q.verdict === "correct" ? "ok" : "no";
        tr.appendChild(td);
      });
      table.appendChild(tr);
    });
    b.appendChild(table);
  }
  b.parentElement.appendChild(feedbackBar(data.message_id));
  scrollDown();
}

/* ---------- timed quiz (one question at a time, no hints or reflections) ---------- */
let quizMsg = null;   // the current quiz question on screen
function renderQuizQuestion(data) {
  const q = data.question, sess = data.session || {};
  if (quizMsg) quizMsg.remove();
  const b = msgShell("Quiz", "tutor");
  quizMsg = b.parentElement;
  b.style.whiteSpace = "normal";
  const shownAt = Date.now();

  const card = document.createElement("div");
  card.className = "qcard";
  const chip = document.createElement("div");
  chip.className = "qcount";
  chip.textContent = `Question ${sess.number} of ${sess.total}`;
  card.appendChild(chip);
  const meta = [];
  if (q.chapter) meta.push("Chapter " + q.chapter);
  if (q.domain) meta.push("Domain " + q.domain + " · " + (DOMAINS[q.domain] || ""));
  meta.push("Single best answer");
  card.insertAdjacentHTML("beforeend", `<div class="qmeta">${meta.join("  ·  ")}</div><div class="qstem"></div>`);
  card.querySelector(".qstem").textContent = q.stem;

  const next = document.createElement("button");
  next.className = "qsubmit";
  next.disabled = true;
  next.textContent = sess.number >= sess.total ? "Finish quiz" : "Next question";
  q.options.forEach((opt, i) => {
    const label = document.createElement("label");
    label.className = "qopt";
    const input = document.createElement("input");
    input.type = "radio"; input.name = "quizopt"; input.value = i;
    input.addEventListener("change", () => { next.disabled = false; });
    const span = document.createElement("span");
    span.textContent = String.fromCharCode(65 + i) + ". " + opt;
    label.appendChild(input); label.appendChild(span);
    card.appendChild(label);
  });

  next.addEventListener("click", async () => {
    const pick = card.querySelector("input[name=quizopt]:checked");
    if (!pick || quizEnding) return;
    card.querySelectorAll("input").forEach((x) => (x.disabled = true));
    next.disabled = true;
    next.textContent = sess.number >= sess.total ? "Writing your report…" : "Loading…";
    try {
      const res = await fetch("/api/quiz/answer", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ study_id: studyId, selected: [parseInt(pick.value, 10)],
                               seconds: (Date.now() - shownAt) / 1000 }),
      });
      if (!res.ok) throw new Error();
      renderResponse(await res.json());
    } catch {
      card.querySelectorAll("input").forEach((x) => (x.disabled = false));
      next.disabled = false;
      next.textContent = "Try again";
    }
  });
  card.appendChild(next);
  b.appendChild(card);
  scrollDown();
}

function renderQuizReport(data) {
  updateBanner(null);
  if (quizMsg) { quizMsg.remove(); quizMsg = null; }
  quizEnding = false;
  const r = data.report || {};
  const b = msgShell("Tutor", "tutor");
  b.style.whiteSpace = "normal";
  b.classList.add("summary-card");

  const h = document.createElement("h3");
  h.textContent = (data.timed_out ? "Time's up — your performance report"
    : data.ended_early ? "Quiz ended early — your performance report"
    : "Quiz complete — your performance report") + (r.chapter ? ` (Chapter ${r.chapter})` : "");
  b.appendChild(h);

  if (r.answered) {
    const pct = r.score != null ? Math.round(r.score * 100) + "%" : "–";
    const stats = document.createElement("div");
    stats.className = "pstats";
    const stat = (num, lab) => {
      const d = document.createElement("div"); d.className = "pstat";
      const n = document.createElement("div"); n.className = "num"; n.textContent = num;
      const l = document.createElement("div"); l.className = "lab"; l.textContent = lab;
      d.appendChild(n); d.appendChild(l); stats.appendChild(d);
    };
    stat(`${r.correct}/${r.planned_questions}`, "correct");
    stat(pct, "score");
    stat(`${r.answered}/${r.planned_questions}`, "answered");
    stat(fmtClock(r.time_used || 0), "time used" + (r.time_limit ? ` of ${Math.round(r.time_limit / 60)} min` : ""));
    if (r.avg_seconds != null) stat(fmtClock(r.avg_seconds), "average per question");
    b.appendChild(stats);

    const dom = Object.entries(r.by_domain || {}).filter(([d]) => d !== "unknown");
    if (dom.length) {
      const head = document.createElement("div");
      head.className = "pstats-heading";
      head.textContent = "By exam domain";
      b.appendChild(head);
      dom.sort().forEach(([d, v]) => {
        const p = v.answered ? Math.round((v.correct / v.answered) * 100) : 0;
        const row = document.createElement("div");
        row.className = "pbar-row";
        row.innerHTML = `<span class="lbl dom">${d}. ${DOMAINS[d] || ""}</span>
          <div class="pbar"><div style="width:${p}%"></div></div>
          <span class="pct">${v.correct}/${v.answered}</span>`;
        b.appendChild(row);
      });
    }
  }

  const t = document.createElement("div");
  t.className = "summary-text bubble md";
  t.innerHTML = renderLightMarkdown(data.text);
  b.appendChild(t);

  if (r.questions && r.questions.length) {
    const head = document.createElement("div");
    head.className = "pstats-heading";
    head.textContent = "Review your answers";
    b.appendChild(head);
    r.questions.forEach((q) => {
      const ok = q.verdict === "correct";
      const det = document.createElement("details");
      det.className = "qreview " + (ok ? "rv-ok" : "rv-no");
      const sum = document.createElement("summary");
      sum.textContent = `${q.number}. ${ok ? "✓ Correct" : "✗ Incorrect"} — ` +
        [q.diagnosis, q.topic && q.topic !== "general" ? q.topic : ""].filter(Boolean).join(" \u00b7 ");
      det.appendChild(sum);
      const stem = document.createElement("div");
      stem.className = "qstem";
      stem.textContent = q.stem;
      det.appendChild(stem);
      const pick = (q.selected || [])[0], key = (q.correct_options || [])[0];
      (q.options || []).forEach((opt, i) => {
        const o = document.createElement("div");
        o.className = "qopt static" + (i === key ? " right" : "") + (i === pick && i !== key ? " wrongpick" : "");
        let tag = "";
        if (i === key) tag = "  (correct answer)";
        if (i === pick) tag += i === key ? "" : "  (your answer)";
        const span = document.createElement("span");
        span.textContent = String.fromCharCode(65 + i) + ". " + opt + tag;
        o.appendChild(span);
        if ((i === key || i === pick) && q.rationales && q.rationales[i]) {
          const why = document.createElement("div");
          why.className = "qwhy-text";
          why.textContent = q.rationales[i];
          o.appendChild(why);
        }
        det.appendChild(o);
      });
      if (q.reasoning_principle) {
        const rp = document.createElement("p");
        rp.className = "muted small";
        rp.textContent = "Reasoning principle: " + q.reasoning_principle;
        det.appendChild(rp);
      }
      if (q.textbook_pointer) {
        const tp = document.createElement("p");
        tp.className = "muted small";
        tp.textContent = q.textbook_pointer;
        det.appendChild(tp);
      }
      b.appendChild(det);
    });
    if (r.unanswered) {
      const u = document.createElement("p");
      u.className = "muted small";
      u.textContent = `${r.unanswered} question(s) were not answered and count as incorrect.`;
      b.appendChild(u);
    }
  }
  if (data.message_id) b.parentElement.appendChild(feedbackBar(data.message_id));
  scrollDown();
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
  if (data.session) {
    const chip = document.createElement("div");
    chip.className = "qcount";
    chip.textContent = `Question ${data.session.number} of ${data.session.total}`;
    card.appendChild(chip);
  }

  const meta = [];
  if (q.chapter) meta.push("Chapter " + q.chapter);
  if (q.domain) meta.push("Domain " + q.domain + " \u00b7 " + (DOMAINS[q.domain] || ""));
  if (q.bloom_level) meta.push(BLOOM_LABELS[q.bloom_level] || q.bloom_level);
  meta.push("Single best answer");
  card.insertAdjacentHTML("beforeend", `<div class="qmeta">${meta.join("  \u00b7  ")}</div>
                    <div class="qstem"></div>
                    <div class="qinstr">${data.instruction}</div>`);
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
    lastCard = { card, selected };
    if (data.type === "feedback") markOptions(card, selected, data.correct_options);
    renderResponse(data);
  } catch {
    pending.remove();
    addTutorText("Something went wrong scoring that answer. Please try again.");
  } finally {
    setBusy(false);
  }
}

/* ---------- MKO reasoning dialogue (before any answer is revealed) ---------- */
let lastCard = null;

function renderProbe(data) {
  const b = msgShell("Tutor", "tutor");
  b.style.whiteSpace = "normal";
  b.classList.add("probe-card", "agent-" + (data.agent || "reasoning"));

  const label = document.createElement("div");
  label.className = "socratic-label";
  label.textContent = (data.agent_label || "Reasoning coach");
  b.appendChild(label);

  const q = document.createElement("div");
  q.className = "probe-text";
  q.textContent = data.message;
  b.appendChild(q);

  const ta = document.createElement("textarea");
  ta.placeholder = "Explain your thinking (type or use the mic)";
  b.appendChild(ta);
  b.appendChild(dictationButton(ta));

  const send = document.createElement("button");
  send.className = "qsubmit";
  send.textContent = "Reply";
  send.addEventListener("click", async () => {
    const reply = ta.value.trim();
    if (!reply) { alert("Share a sentence or two about your thinking."); return; }
    send.disabled = true; ta.disabled = true;
    const pending = addThinking();
    setBusy(true);
    try {
      const res = await fetch("/api/probe", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ study_id: studyId, reply }),
      });
      const out = await res.json();
      pending.remove();
      if (out.type === "feedback" && lastCard) markOptions(lastCard.card, lastCard.selected, out.correct_options);
      renderResponse(out);
    } catch {
      pending.remove();
      addTutorText("Something went wrong sending that. Please try again.");
      send.disabled = false; ta.disabled = false;
    } finally {
      setBusy(false);
    }
  });
  b.appendChild(send);
  scrollDown();
  ta.focus();
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
