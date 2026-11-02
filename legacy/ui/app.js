/* Thesis Ledger — single-page UI. No frameworks, no build step. */
"use strict";

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

const S = {
  week: null,
  state: null,
  view: "week",
  selected: null,
  lastIntake: null,
  intakeBusy: false,     // an intake request is in flight — the poller backs off
  habits: null,          // last GET /api/habits payload (templates + week)
  habitProposals: null,  // parsed chat proposals awaiting review
  habitParseText: "",    // the sentence, kept across renderHabits() re-renders
  habitBusy: false,      // a habits POST is in flight — block double-submits
};

/* ------------------------------------------------------------------ utils */
const pad = (n) => String(n).padStart(2, "0");

function hhmmToMin(s) {
  if (!s) return null;
  const [h, m] = String(s).split(":").map(Number);
  if (Number.isNaN(h) || Number.isNaN(m)) return null;
  return h * 60 + m;
}
const minToHHMM = (m) => `${pad(Math.floor(m / 60))}:${pad(m % 60)}`;

function fmtMinutes(m) {
  m = Math.max(0, Math.round(m));
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60), r = m % 60;
  return r ? `${h}h${pad(r)}` : `${h}h`;
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function isoWeek(dateStr) {
  const d = new Date(dateStr + "T00:00:00");
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const day = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - day);
  const y0 = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  return Math.ceil(((t - y0) / 86400000 + 1) / 7);
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function shortDate(iso) {
  const d = new Date(iso + "T00:00:00");
  return `${d.getDate()} ${MONTHS[d.getMonth()]}`;
}

function weekdayLong(iso) {
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
}

/* -------------------------------------------------------------------- api */
async function api(path, opts) {
  const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { /* non-JSON */ }
  if (!res.ok) {
    const msg = (data && (data.error || data.message)) || `${res.status} ${res.statusText}`;
    throw new Error(msg);
  }
  return data;
}

async function loadState() {
  const q = S.week ? `?week=${encodeURIComponent(S.week)}` : "";
  const data = await api(`/api/state${q}`);
  S.state = data;
  S.week = data.week;
  renderCategories((data.settings || {}).categories);
  renderAll();
}

/* Populate the intake category picker from the university folder names. */
function renderCategories(categories) {
  const sel = $("#intake-category");
  if (!sel || !Array.isArray(categories)) return;
  const current = sel.value;
  sel.innerHTML = categories.map((c) =>
    `<option value="${esc(c)}"${c === "Thesis" ? " selected" : ""}>${esc(c)}</option>`
  ).join("");
  if (categories.includes(current)) sel.value = current;
}

/* ------------------------------------------------------- next-meeting flag */
function mtgFlagButton(mtg, event) {
  const on = mtg && (mtg.id ? mtg.id === event.id : mtg.date === event.date);
  const title = on
    ? "This is the flagged next presentation meeting — click to clear it"
    : "Flag as the next presentation meeting";
  return `<button class="railflag${on ? " on" : ""}" data-mtg-date="${esc(event.date)}"
    data-mtg-title="${esc(event.title)}" data-mtg-calendar="${esc(event.calendar)}"
    data-mtg-id="${esc(event.id || "")}" title="${title}">${on ? "⚑ planning milestone" : "⚑"}</button>`;
}

function mtgDistanceText(meeting) {
  if (!meeting) return "";
  const d = Number(meeting.days_until);
  if (d === 0) return "today";
  if (d === 1) return "tomorrow";
  if (d < 0) return "passed";
  return `in ${d}d`;
}

/* ----------------------------------------------------------------- render */
function renderAll() {
  if (!S.state) return;
  renderMasthead();
  renderRail();
  renderLedger();
  renderTasks();
  renderCalibration();
  if (S.selected) openDrawer(S.selected.id, true);
}

function renderMasthead() {
  const st = S.state;
  $("#week-num").textContent = `W${isoWeek(st.week)}`;
  const days = st.week_days || [];
  const first = days[0], last = days[days.length - 1];
  if (first && last) {
    $("#week-range").textContent = `${shortDate(first.date)} — ${shortDate(last.date)}`;
  }
  const s = st.stats || {};
  const agent = st.agent || {};
  const bits = [];
  if (s.planned_minutes) bits.push(`${fmtMinutes(s.planned_minutes)} planned`);
  if (s.open_tasks) bits.push(`${s.open_tasks} open`);
  let mtgChip = "";
  if (st.meeting) {
    const title = st.meeting.title || "meeting";
    mtgChip = ` <span class="mtgchip" data-mtg-clear="1" title="Next presentation meeting — click to clear">⚑ ${esc(title)} · ${mtgDistanceText(st.meeting)}</span>`;
  }
  $("#statusline").innerHTML =
    `<span class="dot"></span>` +
    `${esc(bits.join(" · ") || "nothing planned")}${mtgChip}`;
}

function renderRail() {
  const st = S.state;
  const today = st.today;
  $("#rail-date").textContent = weekdayLong(today);

  const slots = (st.slots || []).filter((s) => s.date === today);
  const meetings = ((st.calendar && st.calendar.events) || [])
    .filter((e) => e.date === today && !e.all_day);
  const mtg = st.meeting || null;

  const entries = [];
  slots.forEach((s) => entries.push({
    kind: "task", start: s.start, end: s.end, title: s.title,
    minutes: s.minutes, status: s.status, subtask: s.subtask, task_id: s.task_id,
  }));
  meetings.forEach((e) => entries.push({
    kind: "meeting", start: e.start, end: e.end, title: e.title,
    calendar: e.calendar, id: e.id, date: e.date,
  }));
  entries.sort((a, b) => (hhmmToMin(a.start) || 0) - (hhmmToMin(b.start) || 0));

  const nowMin = hhmmToMin(st.now) || 0;
  const dayStart = hhmmToMin((st.settings || {}).day_start) || 540;
  const dayEnd = hhmmToMin((st.settings || {}).day_end) || 1140;

  const out = [];
  let cursor = Math.max(dayStart, entries.length ? (hhmmToMin(entries[0].start) || dayStart) : dayStart);
  let nowPlaced = false;

  const pushNow = () => {
    if (!nowPlaced && nowMin >= dayStart && nowMin <= dayEnd) {
      out.push(`<div class="nowline" role="presentation"></div>`);
      nowPlaced = true;
    }
  };

  entries.forEach((e) => {
    const s = hhmmToMin(e.start), en = hhmmToMin(e.end);
    if (s == null || en == null) return;
    if (s - cursor >= 25) {
      if (!nowPlaced && nowMin < s && nowMin >= cursor) pushNow();
      out.push(`<div class="gaprow"><span class="t"></span><span class="body">${fmtMinutes(s - cursor)} free</span></div>`);
    }
    if (!nowPlaced && nowMin < s) pushNow();

    const isNow = nowMin >= s && nowMin < en;
    if (isNow) nowPlaced = true;
    const meta = e.kind === "meeting"
      ? esc(e.calendar || "calendar")
      : `${fmtMinutes(e.minutes)}${e.subtask ? " · " + esc(e.subtask) : ""}`;
    const flag = e.kind === "meeting"
      ? mtgFlagButton(mtg, e)
      : "";
    out.push(`
      <div class="slotrow is-${e.kind} ${isNow ? "is-now" : ""}" ${e.task_id ? `data-task="${e.task_id}"` : ""}>
        <span class="t">${esc(e.start)}</span>
        <span class="body">
          <span class="ttl">${esc(e.title)}</span>
          <span class="meta">${meta}</span>
        </span>
        ${flag}
      </div>`);
    cursor = Math.max(cursor, en);
  });

  if (!nowPlaced) pushNow();
  if (cursor < dayEnd) {
    out.push(`<div class="gaprow"><span class="t"></span><span class="body">${fmtMinutes(dayEnd - cursor)} free</span></div>`);
  }
  if (!entries.length) {
    out.push(`<p class="ledger-empty" style="margin:0;padding:14px;text-align:left">No blocks or meetings today.</p>`);
  }
  $("#rail-timeline").innerHTML = out.join("");

  // footer: what's next
  const upcoming = entries.find((e) => (hhmmToMin(e.end) || 0) > nowMin);
  const cal = st.calendar || {};
  let foot = "";
  if (upcoming) {
    foot = `<span class="eyebrow">Next</span><strong>${esc(upcoming.start)}</strong> ${esc(upcoming.title)}`;
  } else if (cal.error) {
    foot = `<span class="eyebrow">Calendar</span>${esc(cal.error)}`;
  } else {
    foot = `<span class="eyebrow">Next</span>Nothing left today`;
  }
  $("#rail-foot").innerHTML = foot;

  $("#rail-sub").textContent = `${slots.length} block${slots.length === 1 ? "" : "s"}`
    + (meetings.length ? ` · ${meetings.length} meeting${meetings.length === 1 ? "" : "s"}` : "");
}

function renderLedger() {
  const st = S.state;
  const days = st.week_days || [];
  const dayStart = hhmmToMin((st.settings || {}).day_start) || 540;
  const dayEnd = hhmmToMin((st.settings || {}).day_end) || 1140;
  const span = Math.max(60, dayEnd - dayStart);
  const nowMin = hhmmToMin(st.now) || 0;

  const pct = (m) => Math.max(0, Math.min(100, ((m - dayStart) / span) * 100));

  const hours = [];
  for (let h = Math.ceil(dayStart / 60); h * 60 <= dayEnd; h += 2) {
    hours.push(`<span style="left:${pct(h * 60)}%">${pad(h)}</span>`);
  }

  const html = days.map((d, i) => {
    const slots = (st.slots || []).filter((s) => s.date === d.date);
    const meetings = ((st.calendar && st.calendar.events) || [])
      .filter((e) => e.date === d.date && !e.all_day);
    const load = slots.reduce((a, s) => a + (s.minutes || 0), 0);

    const block = (b, cls, lane) => {
      const s = hhmmToMin(b.start), e = hhmmToMin(b.end);
      if (s == null || e == null) return "";
      const left = pct(Math.max(s, dayStart));
      const right = pct(Math.min(e, dayEnd));
      const w = Math.max(0.9, right - left);
      const label = b.title || "task";
      const mins = b.minutes ? `<span class="bm">${fmtMinutes(b.minutes)}</span>` : "";
      return `<button class="block ${cls}" style="left:${left}%;width:${w}%;top:${lane}px"
        title="${esc(b.title)} · ${esc(b.start)}–${esc(b.end)}"
        ${b.task_id ? `data-task="${b.task_id}"` : ""}>${esc(label)}${mins}</button>`;
    };

    const tasksHtml = slots.map((s) =>
      block(s, `is-${s.status === "done" ? "done" : s.status === "dropped" ? "dropped" : "task"}`, 22)).join("");
    const meetHtml = meetings.map((m) => block(m, "is-meeting", 0)).join("");

    const nowLine = (d.date === st.today && nowMin >= dayStart && nowMin <= dayEnd)
      ? `<span class="nowbar" style="left:${pct(nowMin)}%"></span>` : "";

    const isPast = d.date < st.today;
    const isWeekend = d.is_weekend;

    return `
      <div class="day ${d.is_today ? "is-today" : ""} ${isWeekend ? "is-weekend" : ""} ${isPast ? "is-past" : ""}" style="--i:${i}">
        <div class="daylabel">
          <span class="dow">${esc(d.label)}</span>
          <span class="num">${d.num}</span>
          <span class="load">${load ? fmtMinutes(load) : ""}</span>
        </div>
        <div class="track" style="background-size: calc(100% / ${Math.max(1, Math.round(span / 60))}) 100%">
          ${meetHtml}
          ${tasksHtml}
          ${nowLine}
        </div>
      </div>`;
  }).join("");

  $("#ledger").innerHTML =
    `<div class="ledger-head"><div class="spacer"></div>
       <div class="ruler">${hours.join("")}</div></div>` + html;
  $("#ledger-empty").hidden = (st.slots || []).length > 0;
}

function renderTasks() {
  const st = S.state;
  const tasks = (st.tasks || []).filter((t) => t.status !== "dropped");
  const order = { doing: 0, todo: 1, blocked: 2, done: 3 };
  tasks.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9)
    || (a.priority - b.priority) || a.id - b.id);

  if (!tasks.length) {
    $("#tasklist").innerHTML = `<p class="tasks-empty">No tasks yet. Describe one above.</p>`;
    return;
  }

  $("#tasklist").innerHTML = tasks.map((t, i) => {
    const actual = t.actual_minutes;
    const est = t.est_minutes || 0;
    let num = `<span>${fmtMinutes(est)}</span>`;
    if (actual) {
      const ratio = actual / Math.max(1, est);
      const cls = ratio > 1.15 ? "over" : ratio < 0.85 ? "under" : "";
      num = `<span>${fmtMinutes(est)}</span> <span class="${cls}">→ ${fmtMinutes(actual)}</span>`;
    }
    return `
      <div class="trow" data-task="${t.id}" style="--i:${i}" tabindex="0" role="button">
        <span class="state ${esc(t.status)}" title="${esc(t.status)}"></span>
        <span>
          <span class="ttl">${esc(t.title)}</span>
          <span class="sub">${esc(t.area || "—")}${t.deadline ? " · due " + esc(t.deadline) : ""}</span>
        </span>
        <span class="num">${num}</span>
        <span class="num">${t.confidence ? (Math.round(t.confidence * 100) + "%") : "—"}</span>
        <span class="cx"><b>${esc(t.complexity || "—")}</b></span>
        <span class="cx">P${t.priority || 2}</span>
        <button class="tdel" data-del="${t.id}"
                title="Delete this task — its scheduled blocks and calendar events go too"
                aria-label="Delete task">✕</button>
      </div>`;
  }).join("");
}

async function handleTaskDelete(btn) {
  if (!btn.classList.contains("arm")) {           // first click arms only —
    btn.classList.add("arm");                     // confirm() is unavailable
    btn.textContent = "Delete?";                  // inside the Dock app's WebView
    setTimeout(() => { btn.classList.remove("arm"); btn.textContent = "✕"; },
               4000);
    return;
  }
  btn.disabled = true;
  try {
    const res = await api(`/api/tasks/${btn.dataset.del}`, { method: "DELETE" });
    const u = res.unscheduled || {};
    toast(u.blocks
      ? `Deleted — ${u.blocks} block(s) unscheduled, ${u.calendar_removed} calendar event(s) removed`
      : "Task deleted");
    await loadState();
  } catch (err) {
    btn.disabled = false;
    btn.classList.remove("arm");
    btn.textContent = "✕";
    toast(err.message);
  }
}

function renderCalibration() {
  const st = S.state;
  const cal = st.calibration || { samples: 0 };
  const ratio = cal.ratio || 1;
  const pct = Math.round((ratio - 1) * 100);
  const hedge = cal.samples < 3 ? "Provisional — " : "";
  const verdict = cal.samples < 2
    ? "Not enough feedback yet — report how long two tasks really took."
    : pct > 8 ? `${hedge}your tasks run ${pct}% longer than estimated.`
    : pct < -8 ? `${hedge}your tasks finish ${Math.abs(pct)}% faster than estimated.`
    : "Your estimates are well calibrated.";

  const bars = Object.entries(cal.by_area || {}).map(([area, r]) => {
    const w = Math.max(4, Math.min(100, (r / 2) * 100));
    const cls = r > 1.15 ? "over" : r < 0.85 ? "under" : "";
    return `<div class="bar">
      <span class="lab">${esc(area)}</span>
      <span class="track2"><span class="fill ${cls}" style="width:${w}%"></span></span>
      <span class="val">${r.toFixed(2)}×</span>
    </div>`;
  }).join("") || `<p class="meta">No per-area data yet.</p>`;

  const recent = (cal.recent || []).map((r) => `
    <div class="fbrow">
      <span>${esc(r.title)}</span>
      <span class="m">est ${fmtMinutes(r.estimated)}</span>
      <span class="m">real ${fmtMinutes(r.actual)}</span>
      <span class="m ${r.actual > r.estimated ? "over" : "under"}">${(r.actual / Math.max(1, r.estimated)).toFixed(2)}×</span>
    </div>`).join("") || `<p class="meta">Nothing reported yet.</p>`;

  const budget = st.stats || {};
  $("#calibration").innerHTML = `
    <div class="card">
      <span class="eyebrow">Estimator</span>
      <h2>How wrong am I, usually?</h2>
      <p class="bigratio">${ratio.toFixed(2)}<small> × ACTUAL / ESTIMATE</small></p>
      <p style="margin:6px 0 0;color:var(--ink-soft);font-size:13.5px">${esc(verdict)}</p>
      <div class="bars">${bars}</div>
    </div>
    <div class="card">
      <span class="eyebrow">Recent feedback</span>
      <h2>What you told it</h2>
      <div class="feedback-list">${recent}</div>
      <div class="field">
        <span class="eyebrow">Open load</span>
        <p style="margin:0;font-family:var(--mono);font-size:11px;color:var(--muted)">
          ${budget.open_tasks || 0} open · ${fmtMinutes(budget.open_minutes || 0)} estimated ·
          ${fmtMinutes(budget.planned_minutes || 0)} placed this week
        </p>
      </div>
    </div>
    ${renderReviewCard(st.review)}`;
}

/* The plan's periodic self-review: triggers, verdicts with their why, and a
   manual "review now" — one LLM call, safe adjustments only. */
function renderReviewCard(review) {
  if (!review) {
    return `
    <div class="card">
      <span class="eyebrow">Last review</span>
      <h2>The plan checks itself</h2>
      <p class="meta">Never reviewed. The cycle re-reads the plan when real
      triggers fire — calibration shift, busy minutes moved, deadline
      pressure, task churn, or staleness (>48h) — at most once a day.</p>
      <button class="ghostbtn" id="review-now">Review now</button>
    </div>`;
  }
  const when = review.at ? review.at.replace("T", " ").slice(0, 16) : "";
  const byId = Object.fromEntries(((S.state || {}).tasks || [])
    .map((t) => [t.id, t]));
  const rows = (review.records || []).map((r) => {
    const t = byId[r.task_id] || {};
    return `
      <div class="vrow kind-${esc(r.kind)}">
        <span class="vkind">${esc(r.kind)}</span>
        <span class="ttl">${esc(r.title || t.title || ("#" + r.task_id))}</span>
        <span class="sub">${esc(r.why || "")}${r.change ? " → " + esc(r.change) : ""}</span>
      </div>`;
  }).join("") || `<p class="meta">Nothing to change.</p>`;
  const trig = (review.triggers || []).map((t) => `<li>${esc(t)}</li>`).join("");
  return `
    <div class="card">
      <span class="eyebrow">Last review · ${esc(when)}</span>
      <h2>Why the plan looks like this</h2>
      ${review.notes ? `<p style="margin:4px 0 8px;font-size:13.5px;color:var(--ink-soft)">${esc(review.notes)}</p>` : ""}
      ${review.error ? `<p class="meta" style="color:var(--accent)">Review failed: ${esc(review.error)}</p>` : ""}
      ${review.skipped && !(review.verdicts || []).length ? `<p class="meta">${esc(review.skipped)}.</p>` : ""}
      ${trig ? `<p class="meta">Triggers: <ul class="vtrig">${trig}</ul></p>` : ""}
      <div class="vlist">${rows}</div>
      <button class="ghostbtn" id="review-now">Review now</button>
    </div>`;
}

/* ----------------------------------------------------------------- habits */
const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function habitDaysLabel(weekdays) {
  const ws = new Set(weekdays || []);
  if (ws.size === 7) return "daily";
  if (ws.size === 5 && [0, 1, 2, 3, 4].every((w) => ws.has(w))) return "weekdays";
  if (ws.size === 2 && ws.has(5) && ws.has(6)) return "weekends";
  return (weekdays || []).slice().sort((a, b) => a - b)
    .map((w) => WEEKDAY_LABELS[w]).join(" · ");
}

function habitWindowLabel(h) {
  if (h.start && h.end) return `${h.start}–${h.end}`;
  return `${fmtMinutes(h.minutes || 20)}${h.prefer && h.prefer !== "day" ? `, ${h.prefer}` : ""}`;
}

async function loadHabits() {
  S.habits = await api("/api/habits");
  renderHabits();
}

function renderHabits() {
  const el = $("#habits");
  if (!el) return;
  const h = S.habits || { habits: [], days: [], calendar: "Habits" };

  const proposals = S.habitProposals || [];
  const proposalHtml = proposals.length ? `
    <div class="card habit-proposals">
      <span class="eyebrow">From your sentence</span>
      <h2>Tick what to keep, then save</h2>
      ${proposals.map((p, i) => `
        <label class="hproposal">
          <input type="checkbox" data-proposal="${i}" checked>
          <span class="ttl">${esc(p.name)}</span>
          <span class="sub">${esc(habitDaysLabel(p.weekdays))} · ${esc(habitWindowLabel(p))}</span>
        </label>`).join("")}
      <div class="hrow-btns">
        <button class="primary" id="habits-save-proposals">Save selected</button>
        <button class="ghostbtn" id="habits-discard-proposals">Discard</button>
      </div>
    </div>` : "";

  const dayRows = (h.days || []).map((d) => {
    if (!(d.slots || []).length) return "";
    const rows = d.slots.map((s) => {
      const state = s.done ? "done" : s.skipped ? "skipped" : "";
      return `
        <div class="hslot ${state}" data-hslot="${s.id}">
          <span class="hslot-time">${esc(s.start)}–${esc(s.end)}</span>
          <span class="ttl">${esc(s.name)}</span>
          <span class="hslot-btns">
            <button class="pill${s.done ? " on" : ""}" data-hdone="${s.id}" title="Mark done">✓</button>
            <button class="pill${s.skipped ? " on" : ""}" data-hskip="${s.id}" title="Skip this one">✕</button>
          </span>
        </div>`;
    }).join("");
    const today = new Date().toISOString().slice(0, 10) === d.date;
    return `
      <div class="hday">
        <span class="eyebrow">${WEEKDAY_LABELS[d.weekday]}${today ? " · today" : ""} ${shortDate(d.date)}</span>
        ${rows}
      </div>`;
  }).join("");

  const templates = (h.habits || []).map((t) => `
    <div class="hrow${t.active ? "" : " off"}">
      <span class="ttl">${esc(t.name)}</span>
      <span class="sub">${esc(habitDaysLabel(t.weekdays))} · ${esc(habitWindowLabel(t))}</span>
      <button class="tdel" data-hdel="${t.id}"
              title="Delete this habit for real — every instance and calendar event goes too"
              aria-label="Delete habit">✕</button>
    </div>`).join("") || `<p class="meta">No habits yet — describe them above.</p>`;

  const weekdayBoxes = WEEKDAY_LABELS.map((lab, i) => `
    <label class="hwd"><input type="checkbox" value="${i}">${lab}</label>`).join("");

  el.innerHTML = `
    <div class="card">
      <span class="eyebrow">Intake</span>
      <h2>Describe them in one line</h2>
      <form id="habit-parse-form" class="hparse">
        <input type="text" id="habit-parse-text" autocomplete="off"
               value="${esc(S.habitParseText || "")}"
               placeholder="gym mon wed fri 7-8am, spanish 20min every evening">
        <button class="primary" type="submit">Parse</button>
      </form>
      <p class="meta">One LLM call turns your sentence into proposals — nothing is saved until you tick and save below.</p>
      ${proposalHtml}
    </div>
    <div class="card">
      <span class="eyebrow">This week · mirrored to “${esc(h.calendar)}”</span>
      <h2>Instances
        <button class="ghostbtn hsync" data-hsync
                title="Re-read the Habits calendar and re-create what is missing — heals a wiped or hand-edited calendar">Re-sync</button>
      </h2>
      ${dayRows || `<p class="meta">Nothing materialized this week yet.</p>`}
    </div>
    <div class="card">
      <span class="eyebrow">Templates</span>
      <h2>Recurring commitments</h2>
      ${templates}
      <form id="habit-add-form" class="hadd">
        <input type="text" id="habit-add-name" placeholder="new habit" autocomplete="off" required>
        <span class="hwd-row">${weekdayBoxes}</span>
        <span class="hwd-row">
          <input type="time" id="habit-add-start" title="window start (optional)">
          <input type="time" id="habit-add-end" title="window end (optional)">
          <input type="number" id="habit-add-min" min="5" placeholder="min" style="max-width:70px">
          <select id="habit-add-prefer" title="flexible habits sit in this part of the day">
            <option value="morning">morning</option>
            <option value="day" selected>day</option>
            <option value="evening">evening</option>
          </select>
        </span>
        <button class="primary" type="submit">Add</button>
      </form>
      <p class="meta">Give a clock window (start + end) or a duration — flexible habits take the first free gap in their part of the day.</p>
    </div>`;
}

async function handleHabitState(btn) {
  const id = btn.dataset.hdone || btn.dataset.hskip;
  const done = btn.dataset.hdone ? true : null;
  const skipped = btn.dataset.hskip ? true : null;
  const slot = ((S.habits || {}).days || []).flatMap((d) => d.slots)
    .find((s) => String(s.id) === String(id));
  const wasDone = done !== null && slot && slot.done;
  const wasSkip = skipped !== null && slot && slot.skipped;
  const body = done !== null ? { done: !wasDone } : { skipped: !wasSkip };
  try {
    await api(`/api/habits/slots/${id}`, { method: "POST", body: JSON.stringify(body) });
    if (slot) {
      const off = (done !== null && wasDone) || (skipped !== null && wasSkip);
      toast(off ? `${slot.name}: back to planned`
                : `${slot.name}: ${done !== null ? "done ✓" : "skipped"}`);
    }
    await loadHabits();
  } catch (err) {
    toast(err.message, true);
  }
}

async function handleHabitDelete(btn) {
  if (!btn.classList.contains("arm")) {           // two-click arm — same
    btn.classList.add("arm");                     // pattern as task delete
    btn.textContent = "Delete?";
    setTimeout(() => { btn.classList.remove("arm"); btn.textContent = "✕"; }, 4000);
    return;
  }
  btn.disabled = true;
  try {
    const res = await api(`/api/habits/${btn.dataset.hdel}`, { method: "DELETE" });
    const removed = res.removed_events || 0;
    const left = res.events_left_behind || 0;
    toast(`Habit deleted — every instance gone` +
          (removed ? `, ${removed} calendar event(s) removed` : "") +
          (left ? ` — ${left} event(s) could not be deleted; use Re-sync` : ""),
          left > 0);
    await loadHabits();
  } catch (err) {
    btn.disabled = false;
    btn.classList.remove("arm");
    btn.textContent = "✕";
    toast(err.message, true);
  }
}

async function handleHabitSync() {
  if (S.habitBusy) return;
  S.habitBusy = true;
  try {
    const res = await api("/api/habits/sync", { method: "POST" });
    const rep = (res || {}).materialize || {};
    const m = rep.mirror;
    if (rep.error) {
      toast(`Re-sync failed: ${rep.error}`, true);
    } else if (m && (m.errors || []).length) {
      toast(`Mirror: ${m.errors.length} problem(s) — ${m.errors[0]}`, true);
    } else if (m) {
      const bits = [`${m.mirrored} written`, `${m.updated} moved`];
      if (m.remirrored) bits.push(`${m.remirrored} re-created`);
      toast(`Habits calendar re-synced — ${bits.join(", ")}`);
    } else {
      toast("Habits calendar re-synced");
    }
    await loadHabits();
  } catch (err) {
    toast(err.message, true);
  } finally {
    S.habitBusy = false;
  }
}

// habit mutations re-materialize + re-mirror server-side; their response
// carries the mirror report — surface problems the user would otherwise
// never see (a missing calendar, a denied Automation grant)
function toastMirrorProblems(res) {
  const rep = (res || {}).materialize || {};
  const m = rep.mirror || {};
  const errs = m.errors || [];
  if (rep.error) toast(`Habit step failed: ${rep.error}`, true);
  else if (errs.length) {
    toast(`Calendar mirror: ${errs.length} problem(s) — ${errs[0]}`, true);
  }
}

/* ----------------------------------------------------------------- intake */
async function submitIntake(text) {
  const btn = $("#intake-go");
  const box = $("#analysis");
  const mode = $("#intake-mode") ? $("#intake-mode").value : "api";
  const within = $("#intake-within") ? $("#intake-within").value : "1";
  const category = $("#intake-category") ? $("#intake-category").value : "";
  const body = { description: text, within_weeks: within || null,
                 category: category || null };
  if (mode === "offline") body.mode = "offline";
  btn.disabled = true;
  S.intakeBusy = true;
  box.hidden = false;
  const hints = { api: "Reading your folders and calling the model…",
                  offline: "Estimating offline…" };
  box.innerHTML = `<p class="intake-hint">${hints[mode] || "Reading your folders…"}</p>`;
  try {
    const res = await api("/api/intake", {
      method: "POST",
      body: JSON.stringify(body),
    });
    S.lastIntake = res;
    renderAnalysis(res);
    toast(`Task added — ${fmtMinutes((res.analysis || {}).minutes || 0)} estimated, saved to the ledger`);
    await loadState();
  } catch (err) {
    box.hidden = false;
    box.innerHTML = `<p class="feedback-msg" style="color:var(--accent)">${esc(err.message)}</p>`;
  } finally {
    btn.disabled = false;
    S.intakeBusy = false;
  }
}

function renderAnalysis(res) {
  const a = res.analysis || {};
  const box = $("#analysis");
  const subs = (a.subtasks || []).map((s) =>
    `<li><span>${esc(s.title)}</span><span class="m">${fmtMinutes(s.minutes)}</span></li>`).join("");
  const related = (a.related || []).map((r) =>
    `<span class="chip file" title="${esc(r.why || r.note || "")}">${esc(r.path)}</span>`).join("");
  const why = (a.why || []).map((w) => `<li>${esc(w)}</li>`).join("");

  box.innerHTML = `
    <div class="analysis-top">
      <div class="analysis-est">${fmtMinutes(a.minutes || 0)}<small>estimated</small></div>
      <div class="analysis-body">
        <div class="analysis-h">${esc(a.summary || res.title || "Task")}</div>
        <div class="chips">
          <span class="chip accent">${esc(a.complexity || "—")}</span>
          <span class="chip">${esc(a.area || "general")}</span>
          <span class="chip">P${esc(a.priority || 2)}</span>
          ${a.deep_work ? `<span class="chip">deep work</span>` : ""}
          ${a.confidence ? `<span class="chip">${Math.round(a.confidence * 100)}% sure</span>` : ""}
        </div>
        ${why ? `<ul class="analysis-why">${why}</ul>` : ""}
        ${related ? `<div class="chips">${related}</div>` : ""}
      </div>
    </div>
    ${subs ? `<ul class="subs">${subs}</ul>` : ""}
    <div class="analysis-actions">
      <label class="se-help"><input type="checkbox" id="schedule-calendar" checked>
        Write events to Apple Calendar</label>
      <button class="primary" id="analysis-schedule">Place this week</button>
      <button class="ghost" id="analysis-open">Open task</button>
      <span class="note">Writes to your local DB only.</span>
    </div>`;

  const schedule = $("#analysis-schedule");
  if (schedule) {
    schedule.addEventListener("click", async () => {
      schedule.disabled = true;
      const withEvents = $("#schedule-calendar") ? $("#schedule-calendar").checked : false;
      try {
        toast(withEvents ? "Planning and writing calendar events…" : "Checking your calendar…");
        const data = { week: S.week, save: true, wait: 20, write_calendar: withEvents };
        const result = await api("/api/schedule", {
          method: "POST",
          body: JSON.stringify(data),
        });
        await loadState();
        let note = "Placed in the week";
        const written = result.calendar_written;
        if (written && written.created) note += `, ${written.created} event(s) in Calendar`;
        if (written && (written.failed || written.error)) {
          note += ` — calendar write failed: ${written.error || written.failed}`;
          toast(note, true);
        } else {
          toast(note);
        }
      } catch (err) {
        toast(err.message, true);
      } finally {
        schedule.disabled = false;
      }
    });
  }
  const open = $("#analysis-open");
  if (open) open.addEventListener("click", () => openDrawer(res.task_id));
}

/* ------------------------------------------------------------- slot editor */
function placedSlotRow(s) {
  const pinned = Number(s.pinned) ? `<span class="chip pinchip" title="Moved by hand — re-plans keep this block">pinned</span>` : "";
  if (!s.id) {
    return `<div class="slotedit is-readonly"><span>${esc(s.date)}</span>
      <span class="mm">${esc(s.start)}–${esc(s.end)}</span>
      <span class="mm">${fmtMinutes(s.minutes)}</span></div>`;
  }
  return `
    <div class="slotedit" data-slot="${s.id}"
         data-o-start="${esc(s.start)}" data-o-end="${esc(s.end)}">
      <input type="date" class="se-date" value="${esc(s.date)}" aria-label="Date">
      <input type="time" class="se-start" value="${esc(s.start)}" aria-label="Start">
      <span class="mm sep">–</span>
      <input type="time" class="se-end" value="${esc(s.end)}" aria-label="End">
      <button class="pill" data-act="save-slot" title="Move this block">Move</button>
      <button class="pill accent" data-act="drop-slot" title="Unschedule this block">Remove</button>
      ${pinned}
      <span class="se-msg"></span>
    </div>`;
}

function bindSlotEditor() {
  $$("#drawer .slotedit[data-slot]").forEach((row) => {
    const id = row.dataset.slot;
    const msg = $(".se-msg", row);
    // Moving: changing the date and/or start shifts the end so the block's
    // duration is preserved; editing the end is how the duration is changed.
    row.addEventListener("change", (e) => {
      const tgt = e.target;
      if (!tgt.classList.contains("se-start") &&
          !tgt.classList.contains("se-date") &&
          !tgt.classList.contains("se-end")) return;
      const startEl = $(".se-start", row), endEl = $(".se-end", row);
      if (tgt.classList.contains("se-end")) {
        // the user re-defined the duration — re-baseline on it
        row.dataset.oStart = startEl.value || "";
        row.dataset.oEnd = endEl.value || "";
        return;
      }
      const dur = hhmmToMin(row.dataset.oEnd) - hhmmToMin(row.dataset.oStart);
      if (!startEl.value || !Number.isFinite(dur)) return;
      const newEnd = hhmmToMin(startEl.value) + dur;
      if (newEnd < 24 * 60) endEl.value = minToHHMM(newEnd);
      // crossing midnight: leave the end for the user to set explicitly
      row.dataset.oStart = startEl.value;
      row.dataset.oEnd = endEl.value;
    });
    row.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-act]");
      if (!btn || btn.disabled) return;
      const say = (text) => { msg.textContent = text; };
      if (btn.dataset.act === "save-slot") {
        const date = $(".se-date", row).value;
        const start = $(".se-start", row).value;
        const end = $(".se-end", row).value;
        if (!date) { say("Set a date."); return; }
        if (!start || !end) { say("Set start and end (HH:MM)."); return; }
        if (end <= start) { say("End must come after start."); return; }
        btn.disabled = true;
        say("Moving…");
        try {
          const res = await api(`/api/slots/${id}`, {
            method: "POST",
            body: JSON.stringify({ date, start, end }),
          });
          say((res.calendar && res.calendar.changed) ? "Moved (calendar too)" : "Moved");
          await loadState();
        } catch (err) {
          say(err.message);
          btn.disabled = false;
        }
      } else if (btn.dataset.act === "drop-slot") {
        btn.disabled = true;
        say("Removing…");
        try {
          await api(`/api/slots/${id}`, { method: "DELETE" });
          await loadState();
        } catch (err) {
          say(err.message);
          btn.disabled = false;
        }
      }
    });
  });
}

/* ----------------------------------------------------------------- drawer */
function findTask(id) {
  return (S.state.tasks || []).find((t) => t.id === Number(id));
}

function openDrawer(id, silent) {
  const t = findTask(id);
  if (!t) return;
  S.selected = t;
  const d = $("#drawer");
  const slots = (S.state.slots || []).filter((s) => s.task_id === t.id);
  const subs = (t.subtasks || []).map((s) =>
    `<li><span>${esc(s.title)}</span><span class="m">${fmtMinutes(s.minutes)}</span></li>`).join("");
  const placed = slots.map(placedSlotRow).join("");

  const presets = [15, 30, 45, 60, 90, 120];
  d.innerHTML = `
    <button class="drawer-close" id="drawer-close" aria-label="Close">✕</button>
    <span class="eyebrow">Task #${t.id} · ${esc(t.status)}</span>
    <h2>${esc(t.title)}</h2>
    <div class="meta">${esc(t.area || "general")} · ${esc(t.complexity || "—")} ·
      estimated ${fmtMinutes(t.est_minutes || 0)} · P${t.priority || 2}
      ${t.est_source ? " · est " + esc(t.est_source.replace("agent_", "")) : ""}</div>
    ${t.est_rationale ? `<div class="quote"><span class="eyebrow">Why this size</span>${esc(t.est_rationale)}</div>` : ""}
    ${subs ? `<div class="field"><span class="eyebrow">Subtasks</span><ul class="subs">${subs}</ul></div>` : ""}
    ${placed ? `<div class="field"><span class="eyebrow">Placed</span>${placed}
      <p class="se-help">Wrong time? Change it and save — the block is pinned
      and future re-plans work around it.</p></div>` : ""}

    <div class="field">
      <span class="eyebrow">Plan within</span>
      <div class="btnrow">
        <select id="drawer-within">
          <option value="">any week</option>
          ${[1, 2, 3, 4, 6, 8].map((w) =>
            `<option value="${w}" ${Number(t.within_weeks) === w ? "selected" : ""}>${w === 1 ? "this week only" : "the next " + w + " weeks"}</option>`).join("")}
        </select>
      </div>
      <p class="se-help">How far ahead the planner may place this task. 1 = this
      week only; re-plans never move it further out.</p>
    </div>

    <div class="field">
      <span class="eyebrow">How long did it really take?</span>
      <div class="pillrow" id="took-pills">
        ${presets.map((m) => `<button class="pill" data-min="${m}">${fmtMinutes(m)}</button>`).join("")}
        <button class="pill" data-min="custom">other…</button>
      </div>
      <div id="took-custom" hidden style="margin-top:8px">
        <input type="text" id="took-input" placeholder="e.g. 25m or 1h20">
      </div>
      <div style="margin-top:8px">
        <input type="text" id="took-note" placeholder="what surprised you? (optional)">
      </div>
      <div class="btnrow">
        <button class="primary" id="took-save">Save &amp; recalibrate</button>
        <span class="feedback-msg" id="took-msg"></span>
      </div>
    </div>

    <div class="field">
      <span class="eyebrow">Status</span>
      <div class="btnrow">
        <button class="ghost" data-status="doing">Start</button>
        <button class="ghost" data-status="done">Done</button>
        <button class="ghost" data-status="blocked">Blocked</button>
        <button class="ghost danger" data-status="dropped"
                title="Annul: keeps a strikethrough record, removes its scheduled blocks and their calendar events">Drop</button>
      </div>
    </div>`;

  $("#scrim").hidden = false;
  d.hidden = false;
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-within").addEventListener("change", async () => {
    const raw = $("#drawer-within").value;
    await api(`/api/tasks/${t.id}`, {
      method: "POST",
      body: JSON.stringify({ within_weeks: raw === "" ? null : Number(raw) }),
    });
    await loadState();
    toast(raw === "" ? "Window: any week" : "Window: the next " + raw + " weeks");
  });
  $$("#drawer [data-status]").forEach((b) => b.addEventListener("click", async () => {
    const res = await api(`/api/tasks/${t.id}/status`, {
      method: "POST",
      body: JSON.stringify({ status: b.dataset.status, note: "" }),
    });
    await loadState();
    if (b.dataset.status === "dropped") {
      const u = res.unscheduled || {};
      toast(u.blocks
        ? `Dropped — ${u.blocks} block(s) unscheduled, ${u.calendar_removed} calendar event(s) removed`
        : "Dropped");
      closeDrawer();
    }
  }));

  let chosen = null;
  $$("#took-pills .pill").forEach((p) => p.addEventListener("click", () => {
    $$("#took-pills .pill").forEach((x) => x.classList.remove("on"));
    p.classList.add("on");
    const v = p.dataset.min;
    $("#took-custom").hidden = v !== "custom";
    chosen = v === "custom" ? null : Number(v);
    if (v === "custom") $("#took-input").focus();
  }));

  $("#took-save").addEventListener("click", async () => {
    let minutes = chosen;
    if ($("#took-custom").hidden === false) {
      const raw = $("#took-input").value.trim();
      const parsed = parseDuration(raw);
      if (!parsed) { $("#took-msg").textContent = "Try 25m, 1h20 or 90."; return; }
      minutes = parsed;
    }
    if (!minutes) { $("#took-msg").textContent = "Pick a duration first."; return; }
    try {
      const res = await api(`/api/tasks/${t.id}/feedback`, {
        method: "POST",
        body: JSON.stringify({ minutes, note: $("#took-note").value.trim() }),
      });
      const cal = res.calibration || {};
      $("#took-msg").style.color = "var(--green)";
      $("#took-msg").textContent =
        `Saved. Estimator now ${(cal.ratio || 1).toFixed(2)}× over ${cal.samples || 0} sample(s).`;
      await loadState();
    } catch (err) {
      $("#took-msg").style.color = "var(--accent)";
      $("#took-msg").textContent = err.message;
    }
  });

  if (!silent) $("#drawer-close").focus();
  bindSlotEditor();
}

function closeDrawer() {
  S.selected = null;
  $("#drawer").hidden = true;
  $("#scrim").hidden = true;
}

function parseDuration(raw) {
  if (!raw) return null;
  const s = raw.toLowerCase().trim();
  let m = s.match(/^(\d+)\s*h(?:\s*(\d+))?$/);
  if (m) return Number(m[1]) * 60 + Number(m[2] || 0);
  m = s.match(/^(\d+)\s*(?:m|min)$/);
  if (m) return Number(m[1]);
  m = s.match(/^(\d+)$/);
  if (m) return Number(m[1]);
  return null;
}

/* ------------------------------------------------------------------- misc */
let toastTimer = null;
function toast(msg, bad) {
  const el = $("#statusline");
  if (!el) return;
  const prev = el.innerHTML;
  el.innerHTML = `<span class="dot ${bad ? "bad" : ""}"></span>${esc(msg)}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.innerHTML = prev; }, 4000);
}

function setView(view) {
  S.view = view;
  $$(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.view === view)));
  ["week", "tasks", "habits", "calibration"].forEach((v) => {
    const el = $(`#view-${v}`);
    if (el) el.hidden = v !== view;
  });
  if (view === "week") renderLedger();
  if (view === "habits") loadHabits().catch((e) => toast(e.message, true));
}

/* ------------------------------------------------------------------ theme */
const THEMES = ["auto", "light", "dark"];

function applyTheme(mode) {
  const html = document.documentElement;
  if (mode === "auto") html.removeAttribute("data-theme");
  else html.setAttribute("data-theme", mode);
  const btn = $("#theme");
  if (btn) btn.title = `Theme: ${mode}`;
  try { localStorage.setItem("ledger.theme", mode); } catch (e) { /* private mode */ }
}

function initTheme() {
  let mode = "auto";
  try { mode = localStorage.getItem("ledger.theme") || "auto"; } catch (e) { /* ignore */ }
  const qs = new URLSearchParams(location.search).get("theme");
  if (qs && THEMES.includes(qs)) mode = qs;
  applyTheme(mode);
}

/* ------------------------------------------------------------------- boot */
function boot() {
  initTheme();
  $("#theme").addEventListener("click", () => {
    let mode = "auto";
    try { mode = localStorage.getItem("ledger.theme") || "auto"; } catch (e) { /* ignore */ }
    applyTheme(THEMES[(THEMES.indexOf(mode) + 1) % THEMES.length]);
  });

  $$(".tab").forEach((t) => t.addEventListener("click", () => setView(t.dataset.view)));

  $$("[data-week]").forEach((b) => b.addEventListener("click", async () => {
    const delta = Number(b.dataset.week);
    const d = new Date(S.week + "T00:00:00");
    d.setDate(d.getDate() + delta * 7);
    S.week = d.toISOString().slice(0, 10);
    await loadState();
  }));

  $("#week-today").addEventListener("click", async () => {
    S.week = null;
    await loadState();
  });

  $("#intake").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("#intake-text").value.trim();
    if (!text) { $("#intake-text").focus(); return; }
    submitIntake(text);
  });

  $("#intake-text").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      $("#intake").requestSubmit();
    }
  });

  document.addEventListener("click", (e) => {
    const del = e.target.closest("[data-del]");
    if (del) { handleTaskDelete(del); return; }    // before [data-task]: the
    const el = e.target.closest("[data-task]");    // delete button lives in a row
    if (el) { openDrawer(el.dataset.task); return; }
    if (e.target.id === "scrim") closeDrawer();
  });

  // Apple Calendar and the background cycle all change outside this tab —
  // refresh quietly so both directions of the sync stay visible. Backs off
  // while the drawer is open (edits would be clobbered), while an intake
  // waits, and while the tab is hidden.
  setInterval(() => {
    if (document.hidden || S.intakeBusy || !$("#drawer").hidden) return;
    loadState().catch(() => {});
  }, 20000);
  window.addEventListener("focus", () => {
    if (!S.intakeBusy && $("#drawer").hidden) loadState().catch(() => {});
  });

  // past tasks: everything completed before this app existed, registered by
  // hand so the totals and the calibration ratio see the earlier progress too
  $("#past-toggle").addEventListener("click", () => {
    const f = $("#past-form");
    f.hidden = !f.hidden;
    if (!f.hidden) $("#past-title").focus();
  });
  $("#past-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#past-msg");
    const title = $("#past-title").value.trim();
    const actual = Number($("#past-actual").value);
    if (!title) { msg.textContent = "Give it a title."; return; }
    if (!actual || actual <= 0) {
      msg.textContent = "How long did it really take? (real min)";
      return;
    }
    const payload = {
      title,
      area: $("#past-area").value.trim(),
      est_minutes: Number($("#past-est").value) || undefined,
      actual_minutes: actual,
      done: $("#past-done").value || undefined,
    };
    try {
      await api("/api/tasks/past", { method: "POST",
                                     body: JSON.stringify(payload) });
      msg.textContent = "";
      e.target.reset();
      $("#past-form").hidden = true;
      toast("Past task registered — it counts for the totals and calibration");
      await loadState();
    } catch (err) {
      msg.textContent = err.message;
    }
  });

  // habits: chat-parse into proposals, review checkboxes, then save. The
  // section re-renders in place, so ALL handling is delegated on the
  // persistent #habits container — forms included (submit bubbles); binding
  // to the forms directly would (a) crash boot(): they only exist after the
  // first render, and (b) drop their listeners on every re-render, so a
  // submit would fall through to the browser and reload the page.
  $("#habits").addEventListener("click", async (e) => {
    const doneBtn = e.target.closest("[data-hdone]");
    const skipBtn = e.target.closest("[data-hskip]");
    const delBtn = e.target.closest("[data-hdel]");
    if (doneBtn || skipBtn) { await handleHabitState(doneBtn || skipBtn); return; }
    if (delBtn) { await handleHabitDelete(delBtn); return; }
    if (e.target.closest("[data-hsync]")) { await handleHabitSync(); return; }
    if (e.target.id === "habits-discard-proposals") {
      S.habitProposals = null;
      S.habitParseText = "";
      renderHabits();
      return;
    }
    if (e.target.id === "habits-save-proposals") {
      const checked = $$("#habits [data-proposal]")
        .filter((c) => c.checked)
        .map((c) => S.habitProposals[Number(c.dataset.proposal)]);
      if (!checked.length) { toast("Nothing selected"); return; }
      try {
        const res = await api("/api/habits/save", { method: "POST",
                                                    body: JSON.stringify({ habits: checked }) });
        S.habitProposals = null;
        S.habitParseText = "";
        toast(`Saved ${checked.length} habit(s) — the week re-materialized`);
        toastMirrorProblems(res);
        await loadHabits();
      } catch (err) {
        toast(err.message, true);
      }
    }
  });
  $("#habits").addEventListener("submit", async (e) => {
    e.preventDefault();                       // never a native GET/reload
    if (S.habitBusy) return;                  // one request at a time
    if (e.target.id === "habit-parse-form") {
      const text = $("#habit-parse-text").value.trim();
      if (!text) return;
      S.habitBusy = true;
      const btn = e.target.querySelector(".primary");
      if (btn) btn.disabled = true;
      try {
        const res = await api("/api/habits/parse", { method: "POST",
                                                     body: JSON.stringify({ text }) });
        S.habitProposals = res.proposals || [];
        S.habitParseText = text;              // keep the sentence on re-render
        if (!S.habitProposals.length) toast("No habits found in that sentence");
        renderHabits();
      } catch (err) {
        toast(err.message, true);
      } finally {
        S.habitBusy = false;
      }
    }
    if (e.target.id === "habit-add-form") {
      S.habitBusy = true;
      const weekdays = $$("#habit-add-form .hwd input:checked")
        .map((c) => Number(c.value));
      const payload = {
        name: $("#habit-add-name").value.trim(),
        weekdays,
        minutes: Number($("#habit-add-min").value) || undefined,
        start: $("#habit-add-start").value || "",
        end: $("#habit-add-end").value || "",
        prefer: $("#habit-add-prefer").value,
      };
      try {
        const res = await api("/api/habits", { method: "POST", body: JSON.stringify(payload) });
        toast("Habit added — instances are on this week already");
        toastMirrorProblems(res);
        await loadHabits();
      } catch (err) {
        toast(err.message, true);
      } finally {
        S.habitBusy = false;
      }
    }
  });

  // plan review: "review now" re-reads the plan and applies safe verdicts
  $("#calibration").addEventListener("click", async (e) => {
    if (e.target.id !== "review-now") return;
    const btn = e.target;
    btn.disabled = true;
    btn.textContent = "Reviewing…";
    try {
      const rep = await api("/api/review", { method: "POST", body: "{}" });
      toast(rep.error ? `Review failed: ${rep.error}`
                      : `Review done — ${rep.applied || 0} change(s) applied`);
      await loadState();
    } catch (err) {
      toast(err.message, true);
      btn.disabled = false;
      btn.textContent = "Review now";
    }
  });

  // next-meeting flag: set from a rail meeting row, clear from the masthead chip
  $("#rail-timeline").addEventListener("click", async (e) => {    const btn = e.target.closest("[data-mtg-date]");
    if (!btn) return;
    const on = btn.classList.contains("on");
    await api("/api/meeting", {
      method: "POST",
      body: JSON.stringify(on
        ? { clear: true }
        : { date: btn.dataset.mtgDate, title: btn.dataset.mtgTitle,
            calendar: btn.dataset.mtgCalendar, id: btn.dataset.mtgId }),
    });
    await loadState();
    toast(on ? "Meeting milestone cleared" : "Flagged as the next presentation meeting");
  });

  $("#statusline").addEventListener("click", async (e) => {
    if (!e.target.closest("[data-mtg-clear]")) return;
    await api("/api/meeting", { method: "POST", body: JSON.stringify({ clear: true }) });
    await loadState();
    toast("Meeting milestone cleared");
  });

  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
    if (e.key === "Escape") { closeDrawer(); return; }
    // let browser/native combos through (Cmd+C copy, Cmd+R reload, …) —
    // single-letter shortcuts below fire on bare keys only
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (!typing && e.key === "n") { e.preventDefault(); setView("week"); $("#intake-text").focus(); }
    if (!typing && e.key === "t") { setView("week"); }
    if (!typing && e.key === "c") { setView("calibration"); }
    if (!typing && e.key === "a") { setView("tasks"); }
  });

  loadState().then(() => {
    const params = new URLSearchParams(location.search);
    // deep link: /?desc=write+the+audit[&plan=1] sets up the next task
    const desc = params.get("desc");
    if (desc) {
      $("#intake-text").value = desc;
      if (params.get("plan") === "1") $("#intake").requestSubmit();
      else $("#intake-text").focus();
    }
    const wanted = params.get("task");
    if (wanted) openDrawer(wanted);
    const view = new URLSearchParams(location.search).get("view");
    if (view && ["week", "tasks", "calibration"].includes(view)) setView(view);
  }).catch((err) => {
    $("#statusline").innerHTML = `<span class="dot bad"></span>${esc(err.message)}`;
  });

  // quiet refresh: keeps the now-line and the calendar snapshot honest
  setInterval(() => {
    if (document.hidden) return;
    loadState().catch(() => {});
  }, 60000);
}

document.addEventListener("DOMContentLoaded", boot);
