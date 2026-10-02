/* AI-PRLS instructor review — see students' answers, stated reasoning and
   reflections, and leave coaching direction that the AI tutor follows.
   The instructor key is kept in memory only (never stored in the browser). */

(() => {
  const $ = (sel) => document.querySelector(sel);
  let instructorKey = "";
  let selectedStudent = null;

  const BLOOM = { knowledge: "Knowledge", comprehension: "Comprehension", application: "Application",
                  analysis: "Analysis", synthesis: "Synthesis", evaluation: "Evaluation" };
  const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "–");
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: { "Content-Type": "application/json", "X-Instructor-Key": instructorKey },
    });
    if (!res.ok) {
      let detail = "Request failed (" + res.status + ")";
      try { detail = (await res.json()).detail || detail; } catch {}
      throw new Error(detail);
    }
    return res.json();
  }

  /* ---------- screen switching ---------- */
  $("#instructor-link").addEventListener("click", () => {
    $("#login-screen").hidden = true;
    $("#app").hidden = true;
    $("#instructor-screen").hidden = false;
    $("#instructor-login").hidden = false;
    $("#instructor-dash").hidden = true;
    $("#instructor-key").focus();
  });
  $("#instructor-back").addEventListener("click", () => {
    $("#instructor-screen").hidden = true;
    $("#login-screen").hidden = false;
  });
  $("#instructor-logout").addEventListener("click", () => location.reload());
  $("#instructor-refresh").addEventListener("click", () => {
    loadStudents();
    if (selectedStudent) loadStudent(selectedStudent);
  });

  async function signIn() {
    const err = $("#instructor-error");
    err.hidden = true;
    instructorKey = $("#instructor-key").value;
    try {
      await api("/api/instructor/check");
    } catch (e) {
      instructorKey = "";
      err.textContent = e.message;
      err.hidden = false;
      return;
    }
    $("#instructor-key").value = "";
    $("#instructor-login").hidden = true;
    $("#instructor-dash").hidden = false;
    loadStudents();
  }
  $("#instructor-login-btn").addEventListener("click", signIn);

  // Used by the student login when the instructor key is typed as a Study ID.
  window.openInstructorDashboard = (key) => {
    $("#login-screen").hidden = true;
    $("#app").hidden = true;
    $("#instructor-screen").hidden = false;
    $("#instructor-key").value = key;
    signIn();
  };
  $("#instructor-key").addEventListener("keydown", (e) => { if (e.key === "Enter") signIn(); });

  /* ---------- student list ---------- */
  async function loadStudents() {
    const list = $("#student-list");
    list.textContent = "Loading…";
    let students;
    try { students = await api("/api/instructor/students"); }
    catch (e) { list.textContent = e.message; return; }
    list.textContent = "";
    if (!students.length) { list.appendChild(el("p", "muted", "No students yet.")); return; }
    students.forEach((s) => {
      const b = el("button", "stu-btn" + (s.study_id === selectedStudent ? " sel" : ""));
      b.appendChild(document.createTextNode(s.study_id));
      const acc = s.attempts ? Math.round((s.correct / s.attempts) * 100) + "%" : "–";
      b.appendChild(el("small", null,
        `${s.attempts} answered · ${acc} · ${s.reflections} reflections` +
        (s.active_notes ? ` · ${s.active_notes} coaching note(s)` : "")));
      b.addEventListener("click", () => {
        selectedStudent = s.study_id;
        list.querySelectorAll(".stu-btn").forEach((x) => x.classList.remove("sel"));
        b.classList.add("sel");
        loadStudent(s.study_id);
      });
      list.appendChild(b);
    });
  }

  /* ---------- one student's detail ---------- */
  async function loadStudent(studyId) {
    const d = $("#student-detail");
    d.textContent = "Loading…";
    const q = "?study_id=" + encodeURIComponent(studyId);
    let attempts, sessions, notes;
    try {
      [attempts, sessions, notes] = await Promise.all([
        api("/api/instructor/attempts" + q),
        api("/api/instructor/sessions" + q),
        api("/api/instructor/coach" + q),
      ]);
    } catch (e) { d.textContent = e.message; return; }

    d.textContent = "";
    d.appendChild(el("h3", null, studyId));
    const correct = attempts.filter((a) => a.verdict === "correct").length;
    const scaff = attempts.filter((a) => a.used_scaffold).length;
    const refl = attempts.filter((a) => a.reflection).length;
    d.appendChild(el("p", "muted",
      `${attempts.length} questions answered · ${correct} correct · ` +
      `${scaff} needed a hint · ${refl} reflections · ${sessions.length} chapter session(s)`));

    d.appendChild(coachBox(studyId, notes));

    // Chapter sessions with the summative feedback the student saw
    if (sessions.length) {
      const sec = el("div", "idash-section");
      sec.appendChild(el("p", "nav-label", "Chapter sessions"));
      sessions.forEach((s) => {
        const det = el("details", "session-sum");
        const status = s.completed_at ? (s.ended_early ? "ended early" : "completed") : "in progress";
        det.appendChild(el("summary", null,
          `Chapter ${s.chapter ?? "–"} · ${s.correct}/${s.answered} correct of ` +
          `${s.total_questions} · ${status} · ${fmtTime(s.started_at)}`));
        det.appendChild(el("div", null, s.summary || "(No summary yet.)"));
        sec.appendChild(det);
      });
      d.appendChild(sec);
    }

    // Every attempt: case, choice vs key, stated reasoning, reflection
    const sec = el("div", "idash-section");
    sec.appendChild(el("p", "nav-label", "Answers, reasoning & reflections (newest first)"));
    if (!attempts.length) sec.appendChild(el("p", "muted", "No answers yet."));
    attempts.forEach((a) => sec.appendChild(attemptCard(studyId, a)));
    d.appendChild(sec);
  }

  function attemptCard(studyId, a) {
    const card = el("div", "attempt" + (a.verdict === "correct" ? "" : " wrong"));
    const meta = [fmtTime(a.ts)];
    if (a.chapter) meta.push("Chapter " + a.chapter);
    if (a.domain) meta.push("Domain " + a.domain);
    if (a.topic) meta.push(a.topic);
    card.appendChild(el("div", "qmeta", meta.join("  ·  ")));
    card.appendChild(el("div", "stem", a.stem));

    const ol = el("ol");
    ol.type = "A";
    a.options.forEach((opt, i) => {
      const li = el("li", null, opt);
      if (a.selected.includes(i)) li.classList.add("pick");
      if (a.correct.includes(i)) li.classList.add("key");
      ol.appendChild(li);
    });
    card.appendChild(ol);

    const verdict = a.verdict === "correct" ? "Correct" : "Not quite";
    const extra = [a.used_scaffold ? "needed a scaffold hint first" : "no hint needed"];
    if (a.bloom_level) extra.push("reasoning level: " + (BLOOM[a.bloom_level] || a.bloom_level));
    card.appendChild(field("Result", `${verdict} (${extra.join(", ")})`));
    card.appendChild(field("Student's reasoning", a.explanation || "(none given)"));
    if (a.dialogue && a.dialogue.length) {
      const LBL = { reasoning: "Reasoning coach", expert: "Domain expert", patient: "Client" };
      const f = el("div", "field");
      f.appendChild(el("b", null,
        `Reasoning dialogue — initial reasoning: ${a.reasoning_quality || "–"}, ` +
        `highest support level: ${a.max_support_level ?? "–"} (0 challenge … 3 knowledge support)`));
      const box = el("div", "dialogue");
      a.dialogue.forEach((d) => {
        const p = el("p", d.role === "tutor" ? "t" : "s");
        p.textContent = d.role === "tutor"
          ? `${LBL[d.agent] || "Tutor"} (level ${d.support_level ?? "–"}): ${d.content}`
          : `Student: ${d.content}`;
        box.appendChild(p);
      });
      f.appendChild(box);
      card.appendChild(f);
    }
    card.appendChild(field("Reflection", a.reflection || "(no reflection)"));

    const btn = el("button", "linklike coach-this", "Coach on this answer");
    btn.addEventListener("click", () => {
      const ta = $("#coach-text");
      ta.dataset.attemptId = a.id;
      ta.placeholder = `Coaching about: ${a.topic || "this case"}`;
      ta.focus();
      ta.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    card.appendChild(btn);
    return card;
  }

  function field(label, text) {
    const f = el("div", "field");
    f.appendChild(el("b", null, label));
    f.appendChild(el("div", null, text));
    return f;
  }

  /* ---------- coaching through the AI ---------- */
  function coachBox(studyId, notes) {
    const box = el("div", "coach-box idash-section");
    box.appendChild(el("p", "nav-label", "Coach this student through the AI"));
    box.appendChild(el("p", "muted",
      "Write guidance for the tutor, e.g. “She keeps skipping the evaluation step — " +
      "prompt her to ask what information is still missing before choosing an intervention.” " +
      "The AI will follow it in this student's hints, feedback, explanations and session summary, " +
      "and the student sees the note once, labelled as coaching from their instructor."));
    const ta = el("textarea");
    ta.id = "coach-text";
    box.appendChild(ta);
    if (typeof dictationButton === "function") box.appendChild(dictationButton(ta));
    const save = el("button", "qsubmit", "Send coaching to the AI tutor");
    const status = el("span", "muted");
    status.style.marginLeft = "12px";
    save.addEventListener("click", async () => {
      const note = ta.value.trim();
      if (!note) return;
      save.disabled = true;
      try {
        await api("/api/instructor/coach", {
          method: "POST",
          body: JSON.stringify({ study_id: studyId, note, attempt_id: ta.dataset.attemptId || null }),
        });
        loadStudent(studyId);
        loadStudents();
      } catch (e) {
        status.textContent = e.message;
        save.disabled = false;
      }
    });
    box.appendChild(save);
    box.appendChild(status);

    if (notes.length) {
      box.appendChild(el("p", "nav-label", "Coaching notes")).style.marginTop = "14px";
      notes.forEach((n) => {
        const row = el("div", "note-row" + (n.active ? "" : " off"));
        row.appendChild(el("div", "txt", n.note));
        row.appendChild(el("span", "muted",
          (n.delivered_at ? "seen by student" : "not yet seen") + " · " + fmtTime(n.ts)));
        const t = el("button", "linklike", n.active ? "Retire" : "Reactivate");
        t.addEventListener("click", async () => {
          await api("/api/instructor/coach/" + n.id, {
            method: "POST", body: JSON.stringify({ active: !n.active }),
          });
          loadStudent(studyId);
        });
        row.appendChild(t);
        box.appendChild(row);
      });
    }
    return box;
  }
})();
