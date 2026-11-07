// Planner Web Application Logic
let S = {
  tasks: [],
  habits: [],
  habit_logs: [],
  proposals: [],
  beliefs: [],
  events: [],
  calendars_list: [],
  sources: [],
  categories: [],
  config: {},
  llm_budget: {},
  calendar_status: 'unknown',
  calendar_error: '',
  now: new Date().toISOString(),
  activeTab: 'schedule',
  activeTaskId: null,
  taskFilter: 'all',
  currentMonday: getMonday(new Date()),
};

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

// --- Lifecycle & Init ---
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initTabs();
  initGlobalDismiss();
  initScheduleNav();
  initTasksView();
  initHabitsView();
  initProposalsView();
  initMemoryView();
  initCalendarAuth();
  initLLMSettings();
  initWorkspaceSettings();
  initEventInspector();
  initRealtimeClock();
  
  loadState();
  connectSSE();
});

const THEMES = [
  { id: 'ink',      label: 'Ink',      scheme: 'dark'  },
  { id: 'ledger',   label: 'Ledger',   scheme: 'light' },
  { id: 'midnight', label: 'Midnight', scheme: 'dark'  },
];

function normalizeTheme(t) {
  // Legacy values ('iconic', 'dark') map to the new in-palette dark theme.
  return THEMES.some(x => x.id === t) ? t : 'ink';
}

function initTheme() {
  applyTheme(normalizeTheme(localStorage.getItem('planner_theme')));
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', () => {
      const cur = THEMES.findIndex(x => document.body.classList.contains('theme-' + x.id));
      const next = THEMES[(cur + 1) % THEMES.length];
      localStorage.setItem('planner_theme', next.id);
      applyTheme(next.id);
      showToast(`Theme: ${next.label}`, 'info');
    });
  }
}

function applyTheme(id) {
  const t = THEMES.find(x => x.id === id) || THEMES[0];
  THEMES.forEach(x => document.body.classList.remove('theme-' + x.id));
  document.body.classList.add('theme-' + t.id);
  // Native controls (date/time pickers, scrollbars) follow the theme.
  document.documentElement.style.colorScheme = t.scheme;
  const lbl = document.getElementById('theme-toggle-label');
  if (lbl) lbl.textContent = t.label;
}

function getMonday(d) {
  d = new Date(d);
  const day = d.getDay();
  const diff = d.getDate() - day + (day === 0 ? -6 : 1);
  d.setDate(diff);
  d.setHours(0,0,0,0);
  return d;
}

function fmtDate(d) {
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
}

const pad2 = n => String(n).padStart(2, '0');
const fmtTime = d => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
const DAY_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MON_SHORT = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const fmtNice = d => `${DAY_SHORT[d.getDay()]} ${d.getDate()} ${MON_SHORT[d.getMonth()]}`;

// fetch wrapper that surfaces server-side errors instead of failing silently
async function api(url, opts = {}) {
  const init = { ...opts };
  if (init.body && typeof init.body !== 'string') {
    init.body = JSON.stringify(init.body);
    init.headers = { 'Content-Type': 'application/json', ...(init.headers || {}) };
  }
  const res = await fetch(url, init);
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok || (data && data.error)) {
    throw new Error((data && data.error) || `HTTP ${res.status}`);
  }
  return data;
}

// --- Navigation Tabs ---
function initGlobalDismiss() {
  const openModals = () => [...document.querySelectorAll('.modal-backdrop')]
    .filter(m => m.style.display !== 'none' && m.style.display !== '');
  const dismiss = (m) => {
    const closer = m.querySelector('[id$="-cancel"]') || m.querySelector('[id$="-close"]');
    if (closer) closer.click(); else m.style.display = 'none';
  };
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const mods = openModals();
    if (mods.length) dismiss(mods[mods.length - 1]);
  });
  // Forms people type into (API key, new habit, workspace config) only close via Esc / buttons, not stray clicks.
  const sticky = new Set(['modal-llm', 'modal-habit', 'modal-workspace']);
  document.querySelectorAll('.modal-backdrop').forEach(m => {
    if (sticky.has(m.id)) return;
    m.addEventListener('mousedown', e => { if (e.target === m) dismiss(m); });
  });
}

function initTabs() {
  document.querySelectorAll('.nav-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.tab;
      document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
      tab.classList.add('active');
      const view = document.getElementById(`view-${target}`);
      if (view) view.classList.add('active');
      S.activeTab = target;
      if (target === 'schedule') renderScheduleGrid();
    });
  });
}

// --- API & State ---
async function loadState() {
  try {
    const res = await fetch('/api/state');
    if (!res.ok) return;
    const data = await res.json();
    if (!data.calendars_list || data.calendars_list.length === 0) {
      try {
        const calRes = await fetch('/api/calendar/status');
        if (calRes.ok) {
          const calData = await calRes.json();
          if (calData.calendars && calData.calendars.length) {
            data.calendars_list = calData.calendars;
          }
        }
      } catch (_) {}
    }
    S = { ...S, ...data };
    renderAll();
  } catch (err) {
    console.error('Failed to load state', err);
  }
}

function renderAll() {
  updateStatusPills();
  renderCategories();
  renderTasksList();
  renderHabitsList();
  renderHabitLogs();
  renderProposalsList();
  renderMemoryView();
  renderScheduleGrid();
  if (S.activeTaskId) renderTaskDrawer(S.activeTaskId);
}

function updateStatusPills() {
  // Calendar pill
  const calPill = document.getElementById('cal-status');
  const banner = document.getElementById('perm-banner');
  if (S.calendar_status === 'fullAccess') {
    calPill.classList.remove('warning');
    calPill.querySelector('.status-label').textContent = 'Calendar: Connected';
    if (banner) banner.style.display = 'none';
  } else {
    calPill.classList.add('warning');
    calPill.querySelector('.status-label').textContent = `Calendar: ${S.calendar_status}`;
    if (banner) banner.style.display = 'flex';
  }

  // AI budget
  const b = S.llm_budget || {};
  const prov = b.provider === 'gemini' ? 'Gemini' : (b.provider === 'openrouter' ? 'OpenRouter' : 'AI');
  const modelShort = (b.model || '')
    .replace('openrouter/free', 'Free')
    .replace('gemini-3.8-flash', '3.8 Flash')
    .replace('gemini-3.5-flash', '3.5 Flash')
    .replace('gemini-3.5-flash-lite', '3.5 Lite')
    .replace('gemini-2.5-flash', '2.5 Flash');
  const label = modelShort ? `${prov} (${modelShort}) ${b.used || 0}/${b.limit || 1500}` : `${prov} ${b.used || 0}/${b.limit || 1500}`;
  document.getElementById('ai-budget-text').textContent = label;

  // Workspace / Folders pill
  const wsLbl = document.getElementById('workspace-status-label');
  if (wsLbl) {
    const enabledSources = (S.sources || []).filter(s => s.enabled !== false);
    const catCount = (S.categories || []).length;
    wsLbl.textContent = enabledSources.length
      ? `📁 ${enabledSources.length} Folder${enabledSources.length === 1 ? '' : 's'} · ${catCount} Categor${catCount === 1 ? 'y' : 'ies'}`
      : '📁 Choose Folders…';
  }

  // Badges
  document.getElementById('task-count').textContent = (S.tasks || []).length;
  document.getElementById('habit-count').textContent = (S.habits || []).length;
  
  const pendingProps = (S.proposals || []).filter(p => p.status === 'pending');
  const propBadge = document.getElementById('proposal-count');
  if (pendingProps.length > 0) {
    propBadge.textContent = pendingProps.length;
    propBadge.style.display = 'inline-block';
  } else {
    propBadge.style.display = 'none';
  }
}

function formatCategoryLabel(catId) {
  if (!catId) return 'General';
  const found = (S.categories || []).find(c => c.id === catId);
  if (found && found.label) return found.label;
  if (catId === 'thesis') return 'Thesis';
  return String(catId).replace(/^[^:]+:/, '').title();
}

function renderCategories() {
  const sel = document.getElementById('task-category');
  if (sel && S.categories) {
    const curr = sel.value;
    sel.innerHTML = '<option value="">Auto-detect category / Edit existing task</option>';
    S.categories.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.id;
      const kindLbl = c.kind === 'course' ? 'Subfolder' : 'Project Folder';
      const statusSuffix = c.exists === false ? ' [folder not found]' : '';
      opt.textContent = `${c.label} (${kindLbl} → ${c.calendar || 'Calendar'})${statusSuffix}`;
      sel.appendChild(opt);
    });
    sel.value = curr;
  }

  // Dynamic placeholder based on user's configured categories
  const taskInp = document.getElementById('task-input');
  if (taskInp && S.categories && S.categories.length > 0) {
    const sampleCat = S.categories[0].label;
    taskInp.placeholder = `e.g. Plan next milestone for ${sampleCat}, or 'Move task to Friday at 15:00'...`;
  }

  renderSourcesWidgetBar();
}

function renderSourcesWidgetBar() {
  const bar = document.getElementById('active-sources-bar');
  if (!bar) return;
  bar.innerHTML = '';

  const sources = S.sources || [];
  if (sources.length === 0) {
    bar.innerHTML = `
      <span style="font-size:12px;color:var(--text-muted)">No analysis folders configured yet.</span>
      <button type="button" class="btn btn-sm btn-secondary" id="bar-btn-add-folder">+ Choose Folder for File Analysis…</button>
    `;
    const btn = bar.querySelector('#bar-btn-add-folder');
    if (btn) btn.onclick = () => openWorkspaceModal();
    return;
  }

  const titleSpan = document.createElement('span');
  titleSpan.style.cssText = 'font-size:11px;font-weight:600;color:var(--text-muted);margin-right:2px;';
  titleSpan.textContent = 'Analysis Folders:';
  bar.appendChild(titleSpan);

  sources.forEach(src => {
    if (src.enabled === false) return;
    const chip = document.createElement('div');
    chip.className = `source-widget-chip ${src.exists ? '' : 'missing'}`;
    const icon = src.mode === 'subfolders' ? '📂' : '📁';
    const countTxt = src.exists
      ? (src.mode === 'subfolders' ? `✓ ${src.item_count || 0} subfolders` : `✓ ${src.item_count || 0} items`)
      : '⚠ Not found';
    chip.title = `${src.label} (${src.path || 'no path'})\nTarget Calendar: ${src.calendar || 'Default'}\nClick to change folder or settings`;
    chip.innerHTML = `
      <span>${icon} <strong>${escapeHtml(src.label)}</strong></span>
      <span class="source-widget-path">${escapeHtml(src.path || '(no path)')}</span>
      <span class="source-widget-badge">${escapeHtml(countTxt)}</span>
    `;
    chip.addEventListener('click', () => openWorkspaceModal());
    bar.appendChild(chip);
  });

  const addChip = document.createElement('button');
  addChip.type = 'button';
  addChip.className = 'chip';
  addChip.style.marginLeft = 'auto';
  addChip.textContent = '+ Add / Change Folders';
  addChip.addEventListener('click', () => openWorkspaceModal());
  bar.appendChild(addChip);
}

// --- SSE Real-time Feed ---
function connectSSE() {
  const es = new EventSource('/api/stream');
  es.onmessage = e => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.kind === 'toast') {
        showToast(msg.data.message, msg.data.level);
      } else if (msg.kind === 'agent_trace') {
        appendTraceStep(msg.data);
      } else if (msg.kind === 'tasks_changed' || msg.kind === 'habits_changed' || msg.kind === 'proposals_changed' || msg.kind === 'events_changed' || msg.kind === 'state_changed') {
        loadState();
      }
    } catch (err) {}
  };
}

function showToast(text, level = 'info') {
  const container = document.getElementById('toasts');
  const toast = document.createElement('div');
  toast.className = `toast ${level}`;
  toast.textContent = text;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

function uiConfirm(message, title = "Confirm Action") {
  return new Promise((resolve) => {
    const modal = document.getElementById('modal-custom-confirm');
    if (!modal) return resolve(window.confirm(message));
    document.getElementById('m-confirm-title').textContent = title;
    document.getElementById('m-confirm-msg').textContent = message;
    modal.style.display = 'flex';

    function cleanup(val) {
      modal.style.display = 'none';
      document.getElementById('m-confirm-ok').onclick = null;
      document.getElementById('m-confirm-cancel').onclick = null;
      document.getElementById('m-confirm-close').onclick = null;
      resolve(val);
    }

    document.getElementById('m-confirm-ok').onclick = () => cleanup(true);
    document.getElementById('m-confirm-cancel').onclick = () => cleanup(false);
    document.getElementById('m-confirm-close').onclick = () => cleanup(false);
  });
}

function uiPrompt(message, defaultValue = "", title = "Edit") {
  return new Promise((resolve) => {
    const modal = document.getElementById('modal-custom-prompt');
    if (!modal) return resolve(window.prompt(message, defaultValue));
    const input = document.getElementById('m-prompt-input');
    document.getElementById('m-prompt-title').textContent = title;
    document.getElementById('m-prompt-msg').textContent = message;
    input.value = defaultValue ?? "";
    modal.style.display = 'flex';
    setTimeout(() => { input.focus(); input.select(); }, 50);

    function cleanup(val) {
      modal.style.display = 'none';
      document.getElementById('m-prompt-ok').onclick = null;
      document.getElementById('m-prompt-cancel').onclick = null;
      document.getElementById('m-prompt-close').onclick = null;
      input.onkeydown = null;
      resolve(val);
    }

    document.getElementById('m-prompt-ok').onclick = () => cleanup(input.value.trim());
    document.getElementById('m-prompt-cancel').onclick = () => cleanup(null);
    document.getElementById('m-prompt-close').onclick = () => cleanup(null);
    input.onkeydown = (e) => {
      if (e.key === 'Enter') cleanup(input.value.trim());
      if (e.key === 'Escape') cleanup(null);
    };
  });
}

// --- Schedule / Week View ---
function initScheduleNav() {
  document.getElementById('week-prev').addEventListener('click', () => {
    S.currentMonday.setDate(S.currentMonday.getDate() - 7);
    renderScheduleGrid();
  });
  document.getElementById('week-next').addEventListener('click', () => {
    S.currentMonday.setDate(S.currentMonday.getDate() + 7);
    renderScheduleGrid();
  });
  document.getElementById('week-today').addEventListener('click', () => {
    S.currentMonday = getMonday(new Date());
    renderScheduleGrid();
    scrollToNowLine(true);
  });
  const livePill = document.getElementById('live-status');
  if (livePill) {
    livePill.addEventListener('click', () => {
      const schedTab = document.querySelector('.nav-tab[data-tab="schedule"]');
      if (schedTab && S.activeTab !== 'schedule') schedTab.click();
      S.currentMonday = getMonday(new Date());
      renderScheduleGrid();
      scrollToNowLine(true);
    });
  }
  const btnRefresh = document.getElementById('week-refresh');
  if (btnRefresh) {
    btnRefresh.addEventListener('click', async () => {
      btnRefresh.disabled = true;
      btnRefresh.textContent = '↻ Syncing...';
      try {
        await api('/api/calendar/refresh', { method: 'POST' });
        showToast('Synced all iCloud & Google calendars', 'success');
        await loadState();
      } catch (e) {
        showToast('Calendar sync failed: ' + e.message, 'error');
      } finally {
        btnRefresh.disabled = false;
        btnRefresh.textContent = '↻ Sync';
      }
    });
  }
  document.getElementById('toggle-habits').addEventListener('change', renderScheduleGrid);
  document.getElementById('toggle-meetings').addEventListener('change', renderScheduleGrid);
}

function eventMatchesDay(e, dayStr) {
  const sDate = (e.start || '').slice(0, 10);
  const eDate = (e.end || '').slice(0, 10);
  if (!sDate) return false;
  if (sDate === dayStr) return true;
  if (sDate < dayStr && eDate >= dayStr) {
    if (eDate === dayStr && (e.end || '').slice(11, 16) === '00:00') return false;
    return true;
  }
  return false;
}

function isAllDayOrMultiDay(e) {
  if (e.all_day) return true;
  const sDate = (e.start || '').slice(0, 10);
  const eDate = (e.end || '').slice(0, 10);
  if (sDate && eDate && sDate !== eDate) {
    const durHours = (new Date(e.end) - new Date(e.start)) / 3600000;
    if (durHours >= 20) return true;
  }
  return false;
}

function layoutDayTimedItems(items) {
  const sorted = [...items].sort((a, b) => a.startMins - b.startMins || (b.endMins - b.startMins) - (a.endMins - a.startMins));
  let cluster = [];
  let clusterEnd = -1;

  function flushCluster(group) {
    if (!group.length) return;
    const cols = [];
    group.forEach(item => {
      let placed = false;
      for (let c = 0; c < cols.length; c++) {
        if (cols[c] <= item.startMins) {
          cols[c] = item.endMins;
          item.colIdx = c;
          placed = true;
          break;
        }
      }
      if (!placed) {
        item.colIdx = cols.length;
        cols.push(item.endMins);
      }
    });
    const totalCols = Math.max(1, cols.length);
    group.forEach(item => { item.numCols = totalCols; });
  }

  sorted.forEach(item => {
    if (cluster.length && item.startMins >= clusterEnd) {
      flushCluster(cluster);
      cluster = [];
      clusterEnd = -1;
    }
    cluster.push(item);
    clusterEnd = Math.max(clusterEnd, item.endMins);
  });
  flushCluster(cluster);
  return sorted;
}

function renderScheduleGrid() {
  const mon = new Date(S.currentMonday);
  const sun = new Date(mon);
  sun.setDate(sun.getDate() + 6);

  const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  document.getElementById('week-label').textContent = 
    `${monthNames[mon.getMonth()]} ${mon.getDate()} – ${monthNames[sun.getMonth()]} ${sun.getDate()}, ${mon.getFullYear()}`;

  const grid = document.getElementById('week-grid');
  grid.innerHTML = '';

  const showHabits = document.getElementById('toggle-habits').checked;
  const showMeetings = document.getElementById('toggle-meetings').checked;

  // Default 08–20, widened so early lectures / late sessions are never clipped
  let dayStartHour = 8;
  let dayEndHour = 20;
  const hourHeight = 50;
  {
    const wkStart = new Date(mon), wkEnd = new Date(mon);
    wkEnd.setDate(wkEnd.getDate() + 7);
    const widen = (sStr, eStr) => {
      const s0 = new Date(sStr), e0 = new Date(eStr);
      if (isNaN(s0) || isNaN(e0) || s0 < wkStart || s0 >= wkEnd) return;
      dayStartHour = Math.min(dayStartHour, s0.getHours());
      const crossesMidnight = e0.getDate() !== s0.getDate();
      const eh = crossesMidnight ? 24 : e0.getHours() + (e0.getMinutes() > 0 ? 1 : 0);
      dayEndHour = Math.max(dayEndHour, eh);
    };
    (S.events || []).forEach(ev => { if (!isAllDayOrMultiDay(ev) && ev.start && ev.end) widen(ev.start, ev.end); });
    (S.tasks || []).forEach(t => (t.blocks || []).forEach(b => {
      if (b.status === 'proposed' && b.start && b.end) widen(b.start, b.end);
    }));
    const curNow = new Date();
    if (curNow >= wkStart && curNow < wkEnd) {
      dayStartHour = Math.min(dayStartHour, curNow.getHours());
      dayEndHour = Math.max(dayEndHour, curNow.getHours() + 1);
    }
    dayEndHour = Math.min(24, dayEndHour);
  }

  // Pre-collect events for each of the 7 days so we know if an all-day banner strip is needed
  const daysData = [];
  let maxAllDay = 0;
  for (let i = 0; i < 7; i++) {
    const day = new Date(mon);
    day.setDate(day.getDate() + i);
    const dayStr = fmtDate(day);

    const matchingEvs = (S.events || []).filter(e => {
      if (!eventMatchesDay(e, dayStr)) return false;
      const isHabit = (e.calendar || '').toLowerCase().includes('habits') || (e.notes || '').includes('planner:habit=');
      const isBlock = (e.notes || '').includes('planner:block=');
      if (isHabit && !showHabits) return false;
      if (!isHabit && !isBlock && !showMeetings) return false;
      return true;
    });

    const allDayEvs = matchingEvs.filter(e => isAllDayOrMultiDay(e));
    const timedEvs = matchingEvs.filter(e => !isAllDayOrMultiDay(e));

    const proposedBlocks = [];
    (S.tasks || []).forEach(t => {
      (t.blocks || []).forEach(b => {
        if (b.status === 'proposed' && b.start && b.start.startsWith(dayStr)) {
          proposedBlocks.push({ ...b, task: t, isProposed: true });
        }
      });
    });

    maxAllDay = Math.max(maxAllDay, allDayEvs.length);
    daysData.push({ day, dayStr, allDayEvs, timedEvs, proposedBlocks });
  }

  const stripHeight = maxAllDay > 0 ? (Math.min(maxAllDay, 3) * 20 + 6) : 0;
  const headerOffset = 36 + stripHeight;

  // Time column
  const timeCol = document.createElement('div');
  timeCol.className = 'time-col';
  timeCol.innerHTML = `<div style="height:${headerOffset}px;display:flex;align-items:flex-end;justify-content:flex-end;padding:0 6px 3px 0;font-size:9px;color:var(--text-dim);border-bottom:1px solid var(--border)">${stripHeight > 0 ? 'ALL DAY' : ''}</div>`;
  for (let h = dayStartHour; h < dayEndHour; h++) {
    const slot = document.createElement('div');
    slot.className = 'time-slot';
    slot.textContent = `${String(h).padStart(2,'0')}:00`;
    timeCol.appendChild(slot);
  }
  grid.appendChild(timeCol);

  const todayStr = fmtDate(new Date());
  const now = new Date();
  const dayNames = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  S.lastRenderedDate = todayStr;
  S.gridLayout = { dayStartHour, dayEndHour, hourHeight, headerOffset, todayStr };

  // 7 Days
  for (let i = 0; i < 7; i++) {
    const { day, dayStr, allDayEvs, timedEvs, proposedBlocks } = daysData[i];
    const isToday = dayStr === todayStr;

    const dayCol = document.createElement('div');
    dayCol.className = `day-col ${isToday ? 'today' : ''}`;

    const dayHeader = document.createElement('div');
    dayHeader.className = `day-header ${isToday ? 'today' : ''}`;
    dayHeader.innerHTML = `<span>${dayNames[i]}</span><span style="font-size:12px;opacity:0.8">${day.getDate()}</span>`;
    dayCol.appendChild(dayHeader);

    if (stripHeight > 0) {
      const strip = document.createElement('div');
      strip.className = 'allday-strip';
      strip.style.height = `${stripHeight}px`;
      strip.style.overflowY = allDayEvs.length > 3 ? 'auto' : 'hidden';
      allDayEvs.forEach(e => {
        const chip = document.createElement('div');
        chip.className = 'allday-chip';
        const calShort = (e.calendar || '').trim();
        chip.textContent = `${calShort ? '[' + calShort + '] ' : ''}${e.title || 'Event'}`;
        chip.addEventListener('click', () => openEventInspector(e, false));
        strip.appendChild(chip);
      });
      dayCol.appendChild(strip);
    }

    const lines = document.createElement('div');
    lines.className = 'grid-lines';
    for (let h = dayStartHour; h < dayEndHour; h++) {
      const hl = document.createElement('div');
      hl.className = 'grid-hour-line';
      lines.appendChild(hl);
    }
    dayCol.appendChild(lines);

    // Now line if today (updated continuously in real time)
    if (isToday && now.getHours() >= dayStartHour && now.getHours() < dayEndHour) {
      const nowLine = document.createElement('div');
      nowLine.className = 'now-line';
      nowLine.id = 'cal-now-line';
      const minsFromStart = (now.getHours() - dayStartHour) * 60 + now.getMinutes() + (now.getSeconds() / 60);
      const top = (minsFromStart / 60) * hourHeight + headerOffset;
      nowLine.style.top = `${top}px`;
      nowLine.innerHTML = `<span class="now-line-badge" id="cal-now-badge">${fmtTime(now)}</span>`;
      dayCol.appendChild(nowLine);
    }

    // Combine timed events + proposed blocks for clean side-by-side overlap layout
    const rawItems = [];
    timedEvs.forEach(e => {
      const s = new Date(e.start), en = new Date(e.end);
      const startMins = s.getHours() * 60 + s.getMinutes();
      const endMins = Math.max(startMins + 15, (en.getDate() !== s.getDate() ? 24 * 60 : en.getHours() * 60 + en.getMinutes()));
      const isHabit = (e.calendar || '').toLowerCase().includes('habits') || (e.notes || '').includes('planner:habit=');
      const isBlock = (e.notes || '').includes('planner:block=');
      rawItems.push({ kind: 'event', data: e, tileType: isBlock ? 'block' : (isHabit ? 'habit' : 'meeting'), startMins, endMins });
    });
    proposedBlocks.forEach(b => {
      const s = new Date(b.start), en = new Date(b.end);
      const startMins = s.getHours() * 60 + s.getMinutes();
      const endMins = Math.max(startMins + 15, en.getHours() * 60 + en.getMinutes());
      rawItems.push({ kind: 'block', data: b, tileType: 'block', startMins, endMins });
    });

    const laidOut = layoutDayTimedItems(rawItems);
    laidOut.forEach(item => {
      const tile = item.kind === 'event'
        ? createEventTile(item.data, dayStartHour, hourHeight, item.tileType, headerOffset, item.colIdx, item.numCols)
        : createBlockTile(item.data, dayStartHour, hourHeight, headerOffset, item.colIdx, item.numCols);
      if (tile) dayCol.appendChild(tile);
    });

    grid.appendChild(dayCol);
  }

  renderTodayRail();
  updateRealtimeEventStates(new Date());
  if (!S.didInitialScrollToNow) {
    S.didInitialScrollToNow = true;
    setTimeout(() => scrollToNowLine(false), 60);
  }
}

function scrollToNowLine(smooth = true) {
  const wrap = document.querySelector('.schedule-grid-wrap');
  const nowLine = document.getElementById('cal-now-line');
  if (!wrap || !nowLine) return;
  const lineTop = parseFloat(nowLine.style.top || '0');
  const targetScroll = Math.max(0, lineTop - wrap.clientHeight * 0.38);
  wrap.scrollTo({ top: targetScroll, behavior: smooth ? 'smooth' : 'auto' });
}

function initRealtimeClock() {
  let lastMinute = -1;
  let tickCount = 0;

  const tick = () => {
    const now = new Date();
    S.now = now.toISOString();

    // 1. Update header real-time clock pill
    const clockLbl = document.getElementById('live-clock-label');
    if (clockLbl) {
      clockLbl.textContent = `${fmtNice(now)} · ${pad2(now.getHours())}:${pad2(now.getMinutes())}:${pad2(now.getSeconds())}`;
    }

    // 2. Check midnight rollover so the calendar advances to the new day automatically
    const curDateStr = fmtDate(now);
    if (S.lastRenderedDate && curDateStr !== S.lastRenderedDate) {
      const prevMon = fmtDate(getMonday(new Date(S.lastRenderedDate + 'T12:00:00')));
      if (fmtDate(S.currentMonday) === prevMon) {
        S.currentMonday = getMonday(now);
      }
      S.lastRenderedDate = curDateStr;
      renderScheduleGrid();
      return;
    }

    // 3. Smoothly advance the calendar .now-line and its time badge in real time
    const nowLine = document.getElementById('cal-now-line');
    const nowBadge = document.getElementById('cal-now-badge');
    const layout = S.gridLayout;
    if (nowLine && layout) {
      const h = now.getHours();
      if (h < layout.dayStartHour || h >= layout.dayEndHour) {
        renderScheduleGrid();
        return;
      }
      const minsFromStart = (h - layout.dayStartHour) * 60 + now.getMinutes() + (now.getSeconds() / 60);
      const top = (minsFromStart / 60) * layout.hourHeight + layout.headerOffset;
      nowLine.style.top = `${top}px`;
      if (nowBadge) nowBadge.textContent = fmtTime(now);
    }

    // 4. Every minute rollover, update active/past states on grid tiles & Today rail
    if (now.getMinutes() !== lastMinute) {
      lastMinute = now.getMinutes();
      updateRealtimeEventStates(now);
      renderTodayRail();
    }

    // 5. Every 60s, sync state in background to stay aligned with Apple Calendar
    tickCount += 1;
    if (tickCount % 60 === 0) {
      loadState();
    }
  };

  tick();
  setInterval(tick, 1000);
}

function updateRealtimeEventStates(now = new Date()) {
  const nowMs = now.getTime();
  document.querySelectorAll('.cal-event-tile[data-start][data-end]').forEach(tile => {
    const st = new Date(tile.dataset.start).getTime();
    const en = new Date(tile.dataset.end).getTime();
    if (isNaN(st) || isNaN(en)) return;
    tile.classList.toggle('is-past', en <= nowMs);
    tile.classList.toggle('is-ongoing', st <= nowMs && en > nowMs);
  });
}

function detectVideoProvider(url) {
  if (!url) return null;
  const u = url.toLowerCase();
  if (u.includes('meet.google.com')) return 'Google Meet';
  if (u.includes('zoom.us') || u.includes('zoom.com')) return 'Zoom Call';
  if (u.includes('teams.microsoft.com') || u.includes('teams.live.com')) return 'Teams Call';
  if (u.includes('webex.com')) return 'Webex';
  if (u.includes('jit.si') || u.includes('jitsi')) return 'Jitsi Meet';
  if (u.includes('whereby.com')) return 'Whereby';
  if (u.includes('facetime.apple.com')) return 'FaceTime';
  if (u.includes('discord.gg') || u.includes('discord.com')) return 'Discord';
  return null;
}

function extractEventRichInfo(item, isBlock = false) {
  const rawNotes = item.notes || '';
  const rawLoc = (item.location || '').trim();
  const rawUrl = (item.url || '').trim();

  // Separate internal planner metadata lines from user-facing notes
  const internalTags = [];
  const cleanLines = [];
  let blockId = item.block_id || (isBlock ? item.id : null);
  let taskId = item.task_id || (item.task && item.task.id) || null;
  let habitId = item.habit_id || null;

  rawNotes.split('\n').forEach(line => {
    const trimmed = line.trim();
    const mBlock = trimmed.match(/planner:block=(\d+)/);
    if (mBlock && !blockId) blockId = parseInt(mBlock[1], 10);
    const mTask = trimmed.match(/Planner Task #(\d+)/i) || trimmed.match(/planner:task=(\d+)/);
    if (mTask && !taskId) taskId = parseInt(mTask[1], 10);
    const mHabit = trimmed.match(/planner:habit=(\d+)/);
    if (mHabit && !habitId) habitId = parseInt(mHabit[1], 10);

    if (/^planner:(block|habit|task)=\d+$/i.test(trimmed)) {
      internalTags.push(trimmed);
    } else {
      cleanLines.push(line);
    }
  });

  // Verify blockId against live S.tasks and fall back to event_id or timestamp match if needed
  let verifiedBlock = null;
  (S.tasks || []).forEach(t => {
    (t.blocks || []).forEach(b => {
      if (blockId && b.id === blockId) {
        verifiedBlock = b;
        taskId = t.id;
      }
    });
  });
  if (!verifiedBlock) {
    const itemStart16 = (item.start || '').slice(0, 16);
    (S.tasks || []).forEach(t => {
      if (verifiedBlock) return;
      (t.blocks || []).forEach(b => {
        if (verifiedBlock) return;
        if (item.id && b.event_id && b.event_id === item.id) {
          verifiedBlock = b;
          blockId = b.id;
          taskId = t.id;
        } else if (taskId && t.id === taskId && itemStart16 && (b.start || '').slice(0, 16) === itemStart16) {
          verifiedBlock = b;
          blockId = b.id;
        }
      });
    });
  }

  const linkedTask = taskId ? (S.tasks || []).find(t => t.id === taskId) : null;
  const linkedHabit = habitId
    ? (S.habits || []).find(h => h.id === habitId)
    : ((item.calendar || '').toLowerCase().includes('habits')
      ? (S.habits || []).find(h => (h.name || '').toLowerCase() === (item.title || '').toLowerCase())
      : null);
  if (linkedHabit && !habitId) habitId = linkedHabit.id;

  const cleanNotes = cleanLines.join('\n').trim();

  // Collect all URLs across url, location, and notes
  const urlRegex = /https?:\/\/[^\s<>"')]+/gi;
  const allUrls = [];
  const addUrl = u => {
    const cleaned = u.replace(/[.,;!?]+$/, '');
    if (cleaned && !allUrls.includes(cleaned)) allUrls.push(cleaned);
  };
  if (rawUrl && /^https?:\/\//i.test(rawUrl)) addUrl(rawUrl);
  (rawLoc.match(urlRegex) || []).forEach(addUrl);
  (rawNotes.match(urlRegex) || []).forEach(addUrl);

  let videoUrl = null;
  let videoProvider = null;
  for (const u of allUrls) {
    const prov = detectVideoProvider(u);
    if (prov) {
      videoUrl = u;
      videoProvider = prov;
      break;
    }
  }
  if (!videoUrl && rawUrl && /^https?:\/\//i.test(rawUrl)) {
    const prov = detectVideoProvider(rawUrl);
    if (prov) {
      videoUrl = rawUrl;
      videoProvider = prov;
    }
  }

  // Clean location (if location is just a URL, don't duplicate it as a physical address)
  const cleanLocation = /^https?:\/\/\S+$/i.test(rawLoc) ? '' : rawLoc;

  // Best description snippet for calendar tile preview (strip auto-generated Planner Task headers)
  let descPreview = cleanNotes
    .replace(/^Scheduled work block for task:\s*.+$/gim, '')
    .replace(/^Planner Task #\d+.*$/gim, '')
    .trim();
  if (!descPreview && linkedTask && linkedTask.summary) {
    descPreview = linkedTask.summary.trim();
  }
  if (descPreview && (item.title || '') && descPreview.toLowerCase() === (item.title || '').trim().toLowerCase()) {
    descPreview = '';
  }

  return {
    blockId,
    taskId,
    habitId,
    linkedTask,
    linkedHabit,
    cleanNotes,
    internalTags,
    cleanLocation,
    rawUrl,
    allUrls,
    videoUrl,
    videoProvider,
    descPreview
  };
}

function getTileSizeClass(height) {
  if (height < 32) return 'tile-xs';
  if (height < 46) return 'tile-sm';
  if (height < 72) return 'tile-md';
  return 'tile-lg';
}

function getBlockCategory(item, isBlock = false) {
  const cal = ((item && item.calendar) || '').toLowerCase();
  if (cal.includes('thesis')) return 'thesis';
  if (cal.includes('uni') || cal.includes('erasmus') || cal.includes('sapienza') || cal.includes('course')) return 'university';
  const info = extractEventRichInfo(item, isBlock);
  if (info.linkedTask) {
    if (info.linkedTask.category === 'thesis') return 'thesis';
    const catObj = (S.categories || []).find(c => c.id === info.linkedTask.category);
    if (catObj && catObj.kind === 'thesis') return 'thesis';
    return 'university';
  }
  return 'thesis';
}

function isBlockSubtaskDone(item, isBlock = false) {
  if (!item) return false;
  if (item.status === 'done') return true;
  const info = extractEventRichInfo(item, isBlock);
  if (info.verifiedBlock && info.verifiedBlock.status === 'done') return true;
  if (info.linkedTask) {
    if (info.linkedTask.status === 'done') return true;
    const subId = info.verifiedBlock ? info.verifiedBlock.subtask_id : item.subtask_id;
    if (subId) {
      const sub = (info.linkedTask.subtasks || []).find(s => s.id === subId);
      if (sub && (sub.status === 'done' || sub.done)) return true;
    }
    const title = item.title || '';
    const matched = (info.linkedTask.subtasks || []).find(s => (s.status === 'done' || s.done) && s.title && title.includes(s.title));
    if (matched) return true;
  }
  return false;
}

function createEventTile(e, startHour, hourHeight, type, headerOffset = 36, colIdx = 0, numCols = 1) {
  try {
    const s = new Date(e.start);
    const en = new Date(e.end);
    const startMins = s.getHours() * 60 + s.getMinutes();
    const endMins = en.getDate() !== s.getDate() ? 24 * 60 : (en.getHours() * 60 + en.getMinutes());
    const top = ((startMins - startHour * 60) / 60) * hourHeight + headerOffset;
    const height = Math.max(22, ((endMins - startMins) / 60) * hourHeight);

    const info = extractEventRichInfo(e, false);
    const sizeClass = getTileSizeClass(height);
    const isBlock = type === 'block';
    const catClass = isBlock ? (getBlockCategory(e, false) === 'university' ? 'block-uni' : 'block-thesis') : '';
    const isDone = isBlock && isBlockSubtaskDone(e, false);

    const tile = document.createElement('div');
    tile.className = `cal-event-tile ${type} ${catClass} ${isDone ? 'is-done' : ''} ${sizeClass}`.trim();
    tile.dataset.start = e.start;
    tile.dataset.end = e.end;
    tile.style.top = `${top}px`;
    tile.style.height = `${height}px`;
    tile.style.setProperty('--tile-h', `${height}px`);

    if (numCols > 1) {
      const wPct = 94 / numCols;
      const lPct = 3 + colIdx * wPct;
      tile.style.left = `${lPct}%`;
      tile.style.right = 'auto';
      tile.style.width = `calc(${wPct}% - 2px)`;
    }
    const calName = (e.calendar || '').trim();
    const showCalTag = calName && !['habits'].includes(calName.toLowerCase());

    const metaPills = [];
    if (info.videoUrl) {
      metaPills.push(`<span class="tile-meta-pill">🎥 ${escapeHtml(info.videoProvider || 'Video')}</span>`);
    }
    if (info.cleanLocation) {
      metaPills.push(`<span class="tile-meta-pill">📍 ${escapeHtml(info.cleanLocation)}</span>`);
    }

    const metaHtml = metaPills.length > 0 ? `<div class="tile-meta">${metaPills.join('')}</div>` : '';
    const checkIcon = isDone ? '<span class="tile-check-icon">✓</span>' : '';
    const doneTag = isDone ? ' · <span class="tile-done-badge">Completed</span>' : '';

    tile.innerHTML = `
      <div class="tile-title">${checkIcon}<span class="tile-text">${escapeHtml(e.title || 'Event')}</span></div>
      <div class="tile-time">${e.start.slice(11,16)}–${e.end.slice(11,16)}${showCalTag ? ` · <span class="tile-cal-badge">${escapeHtml(calName)}</span>` : ''}${doneTag}</div>
      ${metaHtml}
    `;
    tile.addEventListener('click', () => {
      openEventInspector(e, false);
    });
    return tile;
  } catch (err) {
    return null;
  }
}

function createBlockTile(b, startHour, hourHeight, headerOffset = 36, colIdx = 0, numCols = 1) {
  try {
    const s = new Date(b.start);
    const en = new Date(b.end);
    const startMins = s.getHours() * 60 + s.getMinutes();
    const endMins = en.getHours() * 60 + en.getMinutes();
    const top = ((startMins - startHour * 60) / 60) * hourHeight + headerOffset;
    const height = Math.max(22, ((endMins - startMins) / 60) * hourHeight);

    const sizeClass = getTileSizeClass(height);
    const catClass = getBlockCategory(b, true) === 'university' ? 'block-uni' : 'block-thesis';
    const isDone = isBlockSubtaskDone(b, true);

    const tile = document.createElement('div');
    tile.className = `cal-event-tile block ${catClass} ${isDone ? 'is-done' : ''} ${sizeClass}`.trim();
    tile.dataset.start = b.start;
    tile.dataset.end = b.end;
    tile.style.top = `${top}px`;
    tile.style.height = `${height}px`;
    tile.style.setProperty('--tile-h', `${height}px`);
    tile.style.borderStyle = b.isProposed ? 'dashed' : 'solid';
    if (numCols > 1) {
      const wPct = 94 / numCols;
      const lPct = 3 + colIdx * wPct;
      tile.style.left = `${lPct}%`;
      tile.style.right = 'auto';
      tile.style.width = `calc(${wPct}% - 2px)`;
    }

    const checkIcon = isDone ? '<span class="tile-check-icon">✓</span>' : '';
    const doneTag = isDone ? ' · <span class="tile-done-badge">Completed</span>' : '';

    tile.innerHTML = `
      <div class="tile-title">${checkIcon}<span class="tile-text">${escapeHtml(b.title)}</span></div>
      <div class="tile-time">${b.start.slice(11,16)}–${b.end.slice(11,16)}${b.isProposed ? ' (Draft)' : ''}${doneTag}</div>
    `;
    tile.addEventListener('click', () => {
      openEventInspector(b, true);
    });
    return tile;
  } catch (err) {
    return null;
  }
}

function renderTodayRail() {
  const container = document.getElementById('today-timeline');
  const todayStr = fmtDate(new Date());
  container.innerHTML = '';

  const dayEvents = (S.events || []).filter(e => eventMatchesDay(e, todayStr))
    .sort((a, b) => (a.all_day ? -1 : 1) - (b.all_day ? -1 : 1) || (a.start || '').localeCompare(b.start || ''));

  if (dayEvents.length === 0) {
    container.innerHTML = '<div class="timeline-item free">No scheduled events for today. Free day!</div>';
    document.getElementById('today-stats').textContent = 'All free';
  } else {
    document.getElementById('today-stats').textContent = `${dayEvents.length} event${dayEvents.length === 1 ? '' : 's'} scheduled today`;
    const nowMs = Date.now();
    dayEvents.forEach(e => {
      const item = document.createElement('div');
      const isHabit = (e.calendar || '').toLowerCase().includes('habits');
      const isBlock = (e.notes || '').includes('planner:block=');
      const catClass = isBlock ? (getBlockCategory(e, false) === 'university' ? 'block-uni' : 'block-thesis') : '';
      const isDone = isBlock && isBlockSubtaskDone(e, false);
      const isAllDay = isAllDayOrMultiDay(e);
      const stMs = e.start ? new Date(e.start).getTime() : 0;
      const enMs = e.end ? new Date(e.end).getTime() : 0;
      const isOngoing = !isAllDay && stMs <= nowMs && enMs > nowMs;
      const isPast = !isAllDay && enMs > 0 && enMs <= nowMs;
      const info = extractEventRichInfo(e, false);
      item.className = `timeline-item ${isBlock ? 'block ' + catClass : (isHabit ? 'habit' : 'meeting')}${isOngoing ? ' is-ongoing' : ''}${isPast ? ' is-past' : ''}${isDone ? ' is-done' : ''}`;
      item.style.cursor = 'pointer';
      const timeLabel = isAllDay ? 'All day' : `${e.start.slice(11,16)}–${e.end.slice(11,16)}`;
      const calLabel = e.calendar ? ` <span class="tile-cal-badge">[${escapeHtml(e.calendar.trim())}]</span>` : '';
      const nowTag = isOngoing ? '<span class="now-pill-tag">NOW</span>' : '';
      const checkTag = isDone ? '<span style="color:var(--success-text);font-weight:700;margin-right:2px">✓</span> ' : '';
      const doneTag = isDone ? ' <span class="tile-done-badge" style="margin-left:4px">Completed</span>' : '';
      const extraBadges = [
        info.videoUrl ? `<span class="tile-cal-badge" style="color:var(--accent)">🎥 ${escapeHtml(info.videoProvider || 'Call')}</span>` : '',
        info.cleanLocation ? `<span class="tile-cal-badge">📍 ${escapeHtml(info.cleanLocation)}</span>` : ''
      ].filter(Boolean).join(' ');
      const titleHtml = isDone ? `<span class="timeline-title-text" style="text-decoration:line-through;opacity:0.85">${escapeHtml(e.title)}</span>` : `<span class="timeline-title-text">${escapeHtml(e.title)}</span>`;
      item.innerHTML = `${nowTag}${checkTag}<strong>${timeLabel}</strong> ${titleHtml}${calLabel}${doneTag}${extraBadges ? `<div style="margin-top:2px;font-size:10px;color:var(--text-muted)">${extraBadges}</div>` : ''}`;
      item.addEventListener('click', () => openEventInspector(e, false));
      container.appendChild(item);
    });
  }

  renderUpcomingCommitments();
  renderInheritedCalendars();
}

function renderUpcomingCommitments() {
  const box = document.getElementById('pi-meeting-info');
  if (!box) return;
  const now = new Date();
  const cutoff = new Date(now.getTime() + 14 * 86400000);
  const upcoming = (S.events || []).filter(e => {
    if (!e.start || !e.end) return false;
    const en = new Date(e.end);
    const st = new Date(e.start);
    if (en < now || st > cutoff) return false;
    const isHabit = (e.calendar || '').toLowerCase().includes('habits') || (e.notes || '').includes('planner:habit=');
    const isBlock = (e.notes || '').includes('planner:block=');
    return !isHabit && !isBlock;
  }).sort((a, b) => a.start.localeCompare(b.start)).slice(0, 7);

  if (upcoming.length === 0) {
    box.innerHTML = '<div style="font-size:12px;color:var(--text-dim)">No external commitments or deadlines in the next 14 days.</div>';
    return;
  }

  box.innerHTML = '';
  upcoming.forEach(e => {
    const row = document.createElement('div');
    row.className = 'upcoming-commit-item';
    const st = new Date(e.start);
    const whenStr = isAllDayOrMultiDay(e)
      ? `${fmtNice(st)} · All day`
      : `${fmtNice(st)} · ${e.start.slice(11,16)}–${e.end.slice(11,16)}`;
    row.innerHTML = `
      <div class="upcoming-commit-when">${escapeHtml(whenStr)} · [${escapeHtml((e.calendar || '').trim())}]</div>
      <div class="upcoming-commit-title">${escapeHtml(e.title || 'Commitment')}</div>
    `;
    row.addEventListener('click', () => {
      S.currentMonday = getMonday(st);
      renderScheduleGrid();
      openEventInspector(e, false);
    });
    box.appendChild(row);
  });
}

function renderInheritedCalendars() {
  const container = document.getElementById('inherited-calendars-list');
  if (!container) return;
  const cals = S.calendars_list || [];
  if (cals.length === 0) {
    container.innerHTML = '<div style="font-size:11px;color:var(--text-dim)">No calendars detected.</div>';
    return;
  }
  const ignoreList = ((S.config && S.config.calendars && S.config.calendars.ignore) || []).map(x => x.trim().toLowerCase());
  container.innerHTML = '';

  cals.forEach(c => {
    const isIgnored = ignoreList.includes((c.name || '').trim().toLowerCase());
    const row = document.createElement('div');
    row.className = 'cal-toggle-row';
    const srcShort = (c.source || '').includes('@') ? 'Uni/Exch' : (c.source || 'Local').replace('Subscribed Calendars', 'Sub');
    row.innerHTML = `
      <label title="${escapeHtml(c.name)} (${escapeHtml(c.source || '')})">
        <input type="checkbox" ${!isIgnored ? 'checked' : ''}>
        <span class="cal-toggle-name">${escapeHtml(c.name)}</span>
      </label>
      <span class="cal-source-badge">${escapeHtml(srcShort)}</span>
    `;
    const chk = row.querySelector('input');
    chk.addEventListener('change', async () => {
      try {
        await api('/api/calendar/toggle', {
          method: 'POST',
          body: { calendar: c.name, enabled: chk.checked }
        });
        showToast(`${chk.checked ? 'Inherited' : 'Hidden'} calendar "${c.name}"`, 'info');
        loadState();
      } catch (err) {
        chk.checked = !chk.checked;
        showToast('Could not toggle calendar: ' + err.message, 'error');
      }
    });
    container.appendChild(row);
  });
}

// --- Tasks View & Agent Intake ---
function initTasksView() {
  const btnPlan = document.getElementById('btn-plan-task');
  const input = document.getElementById('task-input');

  btnPlan.addEventListener('click', () => {
    const text = input.value.trim();
    if (!text) return;
    const cat = document.getElementById('task-category').value;
    startTaskPlanning(text, cat);
  });

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') btnPlan.click();
  });

  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      S.taskFilter = btn.dataset.filter;
      renderTasksList();
    });
  });

  document.getElementById('drawer-close').addEventListener('click', () => {
    document.getElementById('task-drawer').style.display = 'none';
    S.activeTaskId = null;
  });

  document.getElementById('drawer-btn-schedule').addEventListener('click', async () => {
    if (!S.activeTaskId) return;
    try {
      const res = await fetch(`/api/tasks/${S.activeTaskId}/schedule`, { method: 'POST' });
      const data = await res.json();
      if (data.ok) {
        showToast('Task placed into calendar!', 'success');
        loadState();
      }
    } catch (err) {
      showToast('Error scheduling task: ' + err, 'error');
    }
  });

  document.getElementById('drawer-btn-delete').addEventListener('click', async () => {
    if (!S.activeTaskId) return;
    const ok = await uiConfirm('Delete this task and all its calendar events?', 'Delete Task');
    if (!ok) return;
    try {
      await api(`/api/tasks/${S.activeTaskId}`, { method: 'DELETE' });
      showToast('Task deleted', 'info');
      document.getElementById('task-drawer').style.display = 'none';
      S.activeTaskId = null;
      loadState();
    } catch (err) {
      showToast('Error deleting task: ' + err, 'error');
    }
  });

  document.getElementById('btn-save-feedback').addEventListener('click', async () => {
    if (!S.activeTaskId) return;
    const mins = parseInt(document.getElementById('feedback-minutes').value);
    const note = document.getElementById('feedback-note').value;
    if (!mins) return showToast('Enter actual minutes taken', 'warning');
    try {
      const res = await fetch(`/api/tasks/${S.activeTaskId}/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ actual_minutes: mins, note })
      });
      const data = await res.json();
      if (data.ok) {
        showToast('Recalibration saved!', 'success');
        loadState();
      }
    } catch (err) {
      showToast('Error saving feedback: ' + err, 'error');
    }
  });

  const drawerAiInput = document.getElementById('drawer-ai-input');
  const btnDrawerAi = document.getElementById('btn-drawer-ai');
  if (btnDrawerAi && drawerAiInput) {
    btnDrawerAi.addEventListener('click', () => {
      if (!S.activeTaskId) return;
      const text = drawerAiInput.value.trim();
      if (!text) return showToast('Type how you want to adjust this task', 'warning');
      const curTask = (S.tasks || []).find(x => x.id === S.activeTaskId);
      const cat = curTask ? curTask.category : '';
      drawerAiInput.value = '';
      btnDrawerAi.disabled = true;
      btnDrawerAi.textContent = 'Adjusting...';
      startTaskPlanning(text, cat, S.activeTaskId).finally(() => {
        btnDrawerAi.disabled = false;
        btnDrawerAi.textContent = 'Adjust with AI';
      });
    });
    drawerAiInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') btnDrawerAi.click();
    });
  }
}

async function startTaskPlanning(text, category, targetTaskId = null) {
  const btnPlan = document.getElementById('btn-plan-task');
  const input = document.getElementById('task-input');
  const traceBox = document.getElementById('intake-trace-box');
  const traceSteps = document.getElementById('trace-steps');
  const traceStatus = document.getElementById('trace-status');

  if (btnPlan) {
    btnPlan.disabled = true;
    const btnText = btnPlan.querySelector('.btn-text');
    const btnSpinner = btnPlan.querySelector('.btn-spinner');
    if (btnSpinner) btnSpinner.style.display = 'inline-block';
    if (btnText) btnText.textContent = 'Working...';
  }

  traceBox.style.display = 'block';
  traceSteps.innerHTML = '';
  traceStatus.textContent = 'Agent active...';

  try {
    const res = await fetch('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input_text: text, category, task_id: targetTaskId })
    });
    const data = await res.json();
    if (data.ok) {
      if (input && !targetTaskId) input.value = '';
      showToast(targetTaskId ? 'Agent is updating task schedule...' : 'Agent is processing your request...', 'info');
    }
  } catch (err) {
    showToast('Failed to start agent: ' + err, 'error');
  } finally {
    if (btnPlan) {
      btnPlan.disabled = false;
      const btnText = btnPlan.querySelector('.btn-text');
      const btnSpinner = btnPlan.querySelector('.btn-spinner');
      if (btnSpinner) btnSpinner.style.display = 'none';
      if (btnText) btnText.textContent = 'Ask Task Agent';
    }
  }
}

function appendTraceStep(step) {
  const traceSteps = document.getElementById('trace-steps');
  if (!traceSteps) return;
  const el = document.createElement('div');
  el.className = `trace-step ${step.type}`;
  if (step.type === 'thinking') {
    el.textContent = `Turn ${step.turn}: Thinking...`;
  } else if (step.type === 'actions') {
    el.textContent = `Inspecting: ${step.actions.map(a => `${a.tool}(${JSON.stringify(a.args)})`).join(', ')}`;
  } else if (step.type === 'result') {
    el.textContent = `↳ ${step.tool} [${step.ok ? 'OK' : 'Error'}]: ${step.preview}`;
  } else if (step.type === 'final') {
    el.textContent = `✓ Plan ready: ${step.thought}`;
    document.getElementById('trace-status').textContent = 'Complete';
  } else if (step.type === 'error') {
    el.textContent = `Error: ${step.text}`;
  }
  traceSteps.appendChild(el);
  traceSteps.scrollTop = traceSteps.scrollHeight;
}

function renderTasksList() {
  const container = document.getElementById('tasks-container');
  container.innerHTML = '';

  const filter = S.taskFilter;
  const tasks = (S.tasks || []).filter(t => {
    if (filter === 'all') return true;
    return t.status === filter;
  });

  if (tasks.length === 0) {
    container.innerHTML = '<div style="color:var(--text-dim);padding:14px;text-align:center">No tasks in this view</div>';
    return;
  }

  tasks.forEach(t => {
    const card = document.createElement('div');
    const catObj = (S.categories || []).find(c => c.id === t.category);
    const isThesis = t.category === 'thesis' || (catObj && catObj.kind === 'thesis');
    card.className = `task-card ${isThesis ? 'task-card-thesis' : 'task-card-uni'}`;
    const catLabel = formatCategoryLabel(t.category);

    card.innerHTML = `
      <div class="task-card-top">
        <span class="task-card-title">${escapeHtml(t.title || t.input_text)}</span>
        <div style="display:flex;gap:4px;align-items:center">
          <span class="pill pill-${t.status === 'scheduled' ? (isThesis ? 'thesis' : 'course') : 'cx'}">${t.status}</span>
          <button class="btn btn-sm btn-icon btn-card-rename" title="Rename Task">✎</button>
          <button class="btn btn-sm btn-icon btn-card-del" title="Delete Task">✕</button>
        </div>
      </div>
      <div class="task-badges">
        <span class="pill ${isThesis ? 'pill-thesis' : 'pill-course'}">${escapeHtml(catLabel)}</span>
        <span class="pill pill-cx">${t.complexity || 'M'} · ~${t.estimate_minutes || 60}m</span>
        ${t.deadline ? `<span class="pill pill-deadline" style="cursor:pointer" title="Click to edit deadline">Due ${t.deadline.slice(5,16).replace('T',' ')}</span>` : ''}
        ${t.deep_work ? '<span class="pill pill-cx">Deep Work</span>' : ''}
      </div>
    `;

    const dlBadge = card.querySelector('.pill-deadline');
    if (dlBadge) {
      dlBadge.addEventListener('click', (e) => {
        e.stopPropagation();
        openDeadlineDialog(t);
      });
    }

    card.querySelector('.btn-card-rename').addEventListener('click', async (e) => {
      e.stopPropagation();
      const newTitle = await uiPrompt('New title for task:', t.title || t.input_text, 'Rename Task');
      if (newTitle) {
        await api(`/api/tasks/${t.id}/rename`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: newTitle })
        });
        showToast('Task renamed', 'success');
        loadState();
      }
    });

    card.querySelector('.btn-card-del').addEventListener('click', async (e) => {
      e.stopPropagation();
      const ok = await uiConfirm(`Delete task "${t.title || t.input_text}" and remove all its scheduled calendar blocks?`, 'Delete Task');
      if (ok) {
        await api(`/api/tasks/${t.id}`, { method: 'DELETE' });
        showToast('Task deleted', 'info');
        if (S.activeTaskId === t.id) {
          document.getElementById('task-drawer').style.display = 'none';
          S.activeTaskId = null;
        }
        loadState();
      }
    });

    card.addEventListener('click', () => openTaskDrawer(t.id));
    container.appendChild(card);
  });
}

function openTaskDrawer(taskId) {
  S.activeTaskId = taskId;
  renderTaskDrawer(taskId);
}

function renderTaskDrawer(taskId) {
  const t = (S.tasks || []).find(x => x.id === taskId);
  if (!t) return;

  const drawer = document.getElementById('task-drawer');
  drawer.style.display = 'flex';

  document.getElementById('drawer-cat').textContent = formatCategoryLabel(t.category);
  document.getElementById('drawer-title').textContent = t.title || t.input_text;
  document.getElementById('drawer-summary').textContent = t.summary || t.rationale || 'No summary generated.';

  const btnRename = document.getElementById('drawer-btn-rename');
  if (btnRename) {
    btnRename.onclick = async () => {
      const newTitle = await uiPrompt('New title for task:', t.title || t.input_text, 'Rename Task');
      if (newTitle) {
        await api(`/api/tasks/${t.id}/rename`, {
          method: 'POST',
          body: { title: newTitle }
        });
        showToast('Task renamed', 'success');
        loadState();
      }
    };
  }

  const btnDeadline = document.getElementById('drawer-btn-deadline');
  if (btnDeadline) {
    btnDeadline.onclick = () => openDeadlineDialog(t);
  }

  // Badges
  const badges = document.getElementById('drawer-badges');
  badges.innerHTML = `
    <span class="pill pill-cx">Complexity ${t.complexity || 'M'}</span>
    <span class="pill pill-cx">Estimate ~${t.estimate_minutes || 60} min</span>
    <span class="pill pill-p1">P${t.priority || 2}</span>
    ${t.deadline ? `<span class="pill pill-deadline" id="drawer-pill-deadline" style="cursor:pointer" title="Click to edit deadline">Deadline: ${t.deadline.replace('T', ' ')} (${t.deadline_source || 'inferred'})</span>` : ''}
  `;
  const drawerDlPill = document.getElementById('drawer-pill-deadline');
  if (drawerDlPill) {
    drawerDlPill.onclick = () => openDeadlineDialog(t);
  }

  // Files Grounded
  const filesContainer = document.getElementById('drawer-files');
  filesContainer.innerHTML = '';
  const files = t.files || [];
  if (files.length === 0) {
    document.getElementById('drawer-files-sec').style.display = 'none';
  } else {
    document.getElementById('drawer-files-sec').style.display = 'block';
    files.forEach(f => {
      const chip = document.createElement('div');
      chip.className = 'file-chip';
      chip.innerHTML = `<strong>${f.path}</strong><div class="file-why">${f.why}</div>`;
      filesContainer.appendChild(chip);
    });
  }

  // Subtasks
  const subsContainer = document.getElementById('drawer-subtasks');
  subsContainer.innerHTML = '';
  (t.subtasks || []).forEach(st => {
    const isDone = st.status === 'done';
    const row = document.createElement('div');
    row.className = `subtask-row ${isDone ? 'done' : ''}`;
    const actualBadge = isDone && st.actual_minutes ? ` · <strong style="color:var(--accent)">took ${st.actual_minutes}m</strong>` : '';
    row.innerHTML = `
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <input type="checkbox" ${isDone ? 'checked' : ''}>
        <span>${escapeHtml(st.title)} (${st.minutes}m est.${actualBadge})</span>
      </label>
      <div style="display:flex;align-items:center;gap:6px">
        <span style="font-size:10px;color:var(--text-muted)">${escapeHtml(st.notes || '')}</span>
        ${isDone ? `<button type="button" class="btn btn-sm btn-icon btn-edit-sub-mins" title="Edit logged duration for this subtask">✎</button>` : ''}
      </div>
    `;
    const chk = row.querySelector('input');
    const promptSubtaskMinutes = (onCancelRestore) => {
      openCheckinDialog({
        title: isDone ? 'Update Subtask Duration' : 'Complete Subtask',
        itemName: st.title,
        plannedMinutes: st.actual_minutes || st.minutes || 30,
        showDate: false,
        showNote: false,
        onSave: async ({ minutes }) => {
          await api(`/api/tasks/${t.id}/subtasks/${st.id}/done`, {
            method: 'POST',
            body: { actual_minutes: minutes, done: true }
          });
          loadState();
        },
        onCancel: () => {
          if (onCancelRestore) onCancelRestore();
        }
      });
    };

    const editSubBtn = row.querySelector('.btn-edit-sub-mins');
    if (editSubBtn) {
      editSubBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        promptSubtaskMinutes(null);
      });
    }

    chk.addEventListener('change', async () => {
      if (chk.checked) {
        promptSubtaskMinutes(() => { chk.checked = false; });
      } else {
        await api(`/api/tasks/${t.id}/subtasks/${st.id}/done`, {
          method: 'POST',
          body: { done: false }
        });
        showToast(`Reopened subtask '${st.title}' and cleared its calibration entry`, 'info');
        loadState();
      }
    });
    subsContainer.appendChild(row);
  });

  // Blocks
  const blocksContainer = document.getElementById('drawer-blocks');
  blocksContainer.innerHTML = '';
  const blocks = t.blocks || [];
  if (blocks.length === 0) {
    blocksContainer.innerHTML = '<span style="font-size:11px;color:var(--text-dim)">No blocks placed</span>';
  } else {
    if (blocks.length > 1 && (t.subtasks || []).length > 1) {
      const orderBar = document.createElement('div');
      orderBar.style.cssText = 'display:flex;justify-content:flex-end;margin-bottom:6px';
      orderBar.innerHTML = `<button class="btn btn-sm btn-secondary btn-realign-subs" title="Ensure chronological blocks follow strict Subtask 1 → 2 → 3 order">⇅ Realign Subtask Order</button>`;
      orderBar.querySelector('.btn-realign-subs').addEventListener('click', async () => {
        try {
          const res = await api(`/api/tasks/${t.id}/realign_subtasks`, { method: 'POST', body: {} });
          const count = (res.realigned || []).length;
          showToast(count > 0 ? `Realigned ${count} block(s) to match sequential subtask order` : 'Subtask blocks are already in sequential order', 'success');
          loadState();
        } catch (err) {
          showToast('Failed to realign subtasks: ' + err.message, 'error');
        }
      });
      blocksContainer.appendChild(orderBar);
    }
    blocks.forEach(b => {
      const bRow = document.createElement('div');
      bRow.className = 'block-row';
      const bs = new Date(b.start), be = new Date(b.end);
      bRow.innerHTML = `
        <div>
          <div class="br-title">${escapeHtml(b.title)}</div>
          <div class="br-time">${fmtNice(bs)} · ${fmtTime(bs)}–${fmtTime(be)}</div>
        </div>
        <div style="display:flex;gap:4px">
          <button class="btn btn-sm btn-secondary btn-move" title="Change day / time">Reschedule</button>
          <button class="btn btn-sm btn-icon btn-del-b" title="Unschedule this block">✕</button>
        </div>
      `;
      bRow.querySelector('.btn-move').addEventListener('click', () => openRescheduleDialog(t, b));
      bRow.querySelector('.btn-del-b').addEventListener('click', async () => {
        const ok = await uiConfirm('Unschedule this block from calendar?', 'Unschedule Block');
        if (!ok) return;
        try {
          await api(`/api/tasks/${t.id}/blocks/${b.id}`, { method: 'DELETE' });
          showToast('Block unscheduled', 'info');
        } catch (err) {
          showToast('Could not unschedule: ' + err.message, 'error');
        }
        loadState();
      });
      blocksContainer.appendChild(bRow);
    });
  }

  // Pre-fill task recalibration input if previously calibrated
  const fbMinsEl = document.getElementById('feedback-minutes');
  const fbBtnEl = document.getElementById('btn-save-feedback');
  if (fbMinsEl) {
    fbMinsEl.value = t.logged_actual_minutes || '';
    fbMinsEl.placeholder = t.logged_actual_minutes ? `Logged: ${t.logged_actual_minutes}m` : `Minutes (est. ${t.estimate_minutes || 60}m)`;
  }
  if (fbBtnEl) {
    fbBtnEl.textContent = t.logged_actual_minutes ? `Update Recalibration (${t.logged_actual_minutes}m)` : 'Save & Recalibrate';
  }

  // Action button
  const btnSched = document.getElementById('drawer-btn-schedule');
  if (t.status === 'scheduled') {
    btnSched.textContent = '✓ Placed in Apple Calendar';
    btnSched.classList.remove('btn-primary');
    btnSched.classList.add('btn-secondary');
  } else {
    btnSched.textContent = 'Place in Calendar';
    btnSched.classList.remove('btn-secondary');
    btnSched.classList.add('btn-primary');
  }
}


// --- Reschedule dialog (replaces the two raw timestamp prompts) ---
function findConflicts(start, end, block) {
  const hits = [];
  (S.events || []).forEach(e => {
    if (e.all_day || !e.start || !e.end) return;
    if ((e.notes || '').includes(`planner:block=${block.id}`)) return; // the block itself
    const es = new Date(e.start), ee = new Date(e.end);
    if (es < end && ee > start) hits.push(`${e.title || 'Event'} (${fmtTime(es)}–${fmtTime(ee)})`);
  });
  (S.tasks || []).forEach(t => (t.blocks || []).forEach(o => {
    if (o.id === block.id || o.status !== 'proposed') return;
    const os = new Date(o.start), oe = new Date(o.end);
    if (os < end && oe > start) hits.push(`${o.title} (unplaced block, ${fmtTime(os)}–${fmtTime(oe)})`);
  }));
  return hits;
}

function openRescheduleDialog(task, block) {
  const modal = document.getElementById('modal-resched');
  const $ = id => document.getElementById(id);
  const dateEl = $('resched-date'), startEl = $('resched-start'), minEl = $('resched-minutes');
  const origStart = new Date(block.start), origEnd = new Date(block.end);
  const origMin = Math.max(10, Math.round((origEnd - origStart) / 60000));

  $('resched-title').textContent = `Reschedule · ${block.title}`;
  $('resched-from').textContent = `Currently ${fmtNice(origStart)} · ${fmtTime(origStart)}–${fmtTime(origEnd)}`;
  dateEl.value = fmtDate(origStart);
  startEl.value = fmtTime(origStart);
  minEl.value = origMin;

  const selection = () => {
    if (!dateEl.value || !startEl.value) return null;
    const mins = parseInt(minEl.value, 10);
    if (!mins || mins < 5) return null;
    const start = new Date(`${dateEl.value}T${startEl.value}:00`);
    if (isNaN(start)) return null;
    return { start, end: new Date(start.getTime() + mins * 60000), mins };
  };

  const refresh = () => {
    const sel = selection();
    const save = $('resched-save'), warn = $('resched-warn'), ok = $('resched-ok');
    warn.classList.remove('visible'); ok.classList.remove('visible');
    if (!sel) {
      $('resched-when').textContent = 'Pick a valid date, time and duration';
      $('resched-dur-label').textContent = '';
      save.disabled = true;
      return;
    }
    save.disabled = false;
    $('resched-when').textContent = `${fmtNice(sel.start)} · ${fmtTime(sel.start)} – ${fmtTime(sel.end)}`;
    const h = Math.floor(sel.mins / 60), m = sel.mins % 60;
    $('resched-dur-label').textContent = `${h ? h + ' h ' : ''}${m ? m + ' min' : ''}`.trim();
    const hits = findConflicts(sel.start, sel.end, block);
    if (hits.length) {
      warn.innerHTML = 'Overlaps with: ' + hits.map(escapeHtml).join(' · ');
      warn.classList.add('visible');
      save.textContent = 'Move anyway';
    } else {
      ok.classList.add('visible');
      save.textContent = 'Move block';
    }
  };

  const setStart = (d) => { dateEl.value = fmtDate(d); startEl.value = fmtTime(d); refresh(); };

  modal.querySelectorAll('[data-shift-min]').forEach(btn => {
    btn.onclick = () => {
      const sel = selection(); if (!sel) return;
      setStart(new Date(sel.start.getTime() + parseInt(btn.dataset.shiftMin, 10) * 60000));
    };
  });

  $('resched-next-free').onclick = () => {
    const sel = selection(); if (!sel) return;
    const DAY_START = 8, DAY_END = 20;
    let cand = new Date(sel.start.getTime() + 15 * 60000);
    for (let guard = 0; guard < 14 * 24 * 4; guard++) {
      const startMin = cand.getHours() * 60 + cand.getMinutes();
      const end = new Date(cand.getTime() + sel.mins * 60000);
      const endMin = startMin + sel.mins;
      if (startMin < DAY_START * 60) { cand.setHours(DAY_START, 0, 0, 0); continue; }
      if (endMin > DAY_END * 60) { cand.setDate(cand.getDate() + 1); cand.setHours(DAY_START, 0, 0, 0); continue; }
      if (cand > new Date() && findConflicts(cand, end, block).length === 0) { setStart(cand); return; }
      cand = new Date(cand.getTime() + 15 * 60000);
    }
    showToast('No free slot found in the next 2 weeks', 'warning');
  };

  [dateEl, startEl, minEl].forEach(el => { el.oninput = refresh; el.onchange = refresh; });

  const close = () => {
    modal.style.display = 'none';
    ['resched-close', 'resched-cancel', 'resched-save'].forEach(id => { $(id).onclick = null; });
    modal.onkeydown = null;
  };
  const save = async () => {
    const sel = selection(); if (!sel) return;
    const iso = d => `${fmtDate(d)}T${fmtTime(d)}:00`;
    const btn = $('resched-save');
    btn.disabled = true; btn.textContent = 'Moving…';
    try {
      if (block.isCalendarEvent && !block.id_is_block) {
        await api('/api/calendar/events/move', {
          method: 'POST',
          body: {
            event_id: block.id,
            item_id: block.item_id,
            ext_id: block.ext_id,
            occurrence: block.occurrence,
            span: 'this',
            new_start: iso(sel.start),
            new_end: iso(sel.end)
          }
        });
      } else {
        const tid = (task && task.id) || block.task_id || 0;
        await api(`/api/tasks/${tid}/move_block`, {
          method: 'POST',
          body: { block_id: block.block_id || block.id, new_start: iso(sel.start), new_end: iso(sel.end) },
        });
      }
      showToast(`Moved to ${fmtNice(sel.start)} ${fmtTime(sel.start)}`, 'success');
      close();
      loadState();
    } catch (err) {
      showToast('Could not move: ' + err.message, 'error');
      btn.disabled = false; refresh();
    }
  };
  $('resched-close').onclick = close;
  $('resched-cancel').onclick = close;
  $('resched-save').onclick = save;
  modal.onkeydown = e => { if (e.key === 'Enter' && e.target.tagName !== 'BUTTON') { e.preventDefault(); save(); } };

  modal.style.display = 'flex';
  refresh();
  setTimeout(() => startEl.focus(), 40);
}

// --- Structured Deadline Dialog (Date + Time boxes & Quick Chips) ---
function openDeadlineDialog(task) {
  const modal = document.getElementById('modal-deadline');
  if (!modal || !task) return;
  const $ = id => document.getElementById(id);
  const dateEl = $('dl-date'), timeEl = $('dl-time'), replanEl = $('dl-replan');
  const warnEl = $('dl-warn'), clearBtn = $('dl-clear'), saveBtn = $('dl-save');

  $('dl-title').textContent = `Deadline · ${task.title || task.input_text || 'Task'}`;
  const rawDl = (task.deadline || '').trim();
  let initDate, initTime = '23:59';
  if (rawDl && rawDl.length >= 10) {
    initDate = rawDl.slice(0, 10);
    if (rawDl.length >= 16) initTime = rawDl.slice(11, 16);
    $('dl-from').textContent = `Current deadline: ${rawDl.replace('T', ' ')} (${task.deadline_source || 'set'})`;
    clearBtn.style.display = 'inline-flex';
  } else {
    const defD = new Date();
    defD.setDate(defD.getDate() + 3);
    initDate = fmtDate(defD);
    initTime = '23:59';
    $('dl-from').textContent = 'No deadline currently set';
    clearBtn.style.display = 'none';
  }

  dateEl.value = initDate;
  timeEl.value = initTime;
  replanEl.checked = false;

  const getSelection = () => {
    if (!dateEl.value) return null;
    const tVal = timeEl.value || '23:59';
    const dt = new Date(`${dateEl.value}T${tVal}:00`);
    if (isNaN(dt)) return null;
    return { dt, iso: `${dateEl.value}T${tVal}`, dateStr: dateEl.value, timeStr: tVal };
  };

  const refresh = () => {
    const sel = getSelection();
    warnEl.classList.remove('visible');
    if (!sel) {
      $('dl-when').textContent = 'Pick a date and time';
      $('dl-rel-label').textContent = '';
      saveBtn.disabled = true;
      return;
    }
    saveBtn.disabled = false;
    $('dl-when').textContent = `${fmtNice(sel.dt)} · ${sel.timeStr}`;
    const diffHours = Math.round((sel.dt - new Date()) / 3600000);
    if (diffHours < 0) {
      $('dl-rel-label').textContent = `${Math.abs(diffHours)}h in the past`;
    } else if (diffHours < 48) {
      $('dl-rel-label').textContent = `in ${diffHours} h`;
    } else {
      $('dl-rel-label').textContent = `in ${Math.round(diffHours / 24)} days`;
    }

    const lateBlocks = (task.blocks || []).filter(b => b.end && new Date(b.end) > sel.dt);
    if (lateBlocks.length > 0 && !replanEl.checked) {
      warnEl.textContent = `${lateBlocks.length} scheduled block(s) currently end after this deadline. Check "Automatically re-place task blocks" below to reschedule them before the deadline.`;
      warnEl.classList.add('visible');
    }
  };

  modal.querySelectorAll('[data-dl-preset]').forEach(btn => {
    btn.onclick = () => {
      const d = new Date();
      if (btn.dataset.dlPreset === 'tomorrow') d.setDate(d.getDate() + 1);
      dateEl.value = fmtDate(d);
      timeEl.value = '23:59';
      refresh();
    };
  });

  modal.querySelectorAll('[data-dl-shift-days]').forEach(btn => {
    btn.onclick = () => {
      const sel = getSelection() || { dt: new Date() };
      const d = new Date(sel.dt);
      d.setDate(d.getDate() + parseInt(btn.dataset.dlShiftDays, 10));
      dateEl.value = fmtDate(d);
      refresh();
    };
  });

  modal.querySelectorAll('[data-dl-time]').forEach(btn => {
    btn.onclick = () => {
      timeEl.value = btn.dataset.dlTime;
      refresh();
    };
  });

  [dateEl, timeEl, replanEl].forEach(el => { el.oninput = refresh; el.onchange = refresh; });

  const close = () => {
    modal.style.display = 'none';
    ['dl-close', 'dl-cancel', 'dl-save', 'dl-clear'].forEach(id => { $(id).onclick = null; });
    modal.onkeydown = null;
  };

  const applyDeadline = async (dlIso, replan) => {
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    try {
      await api(`/api/tasks/${task.id}/deadline`, {
        method: 'POST',
        body: { deadline: dlIso, replan: !!replan }
      });
      showToast(dlIso ? `Deadline set to ${dlIso.replace('T', ' ')}` : 'Deadline cleared', 'success');
      close();
      loadState();
    } catch (err) {
      showToast('Failed to update deadline: ' + err.message, 'error');
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save Deadline';
    }
  };

  $('dl-close').onclick = close;
  $('dl-cancel').onclick = close;
  clearBtn.onclick = () => applyDeadline('', false);
  saveBtn.onclick = () => {
    const sel = getSelection();
    if (!sel) return;
    applyDeadline(sel.iso, replanEl.checked);
  };
  modal.onkeydown = e => {
    if (e.key === 'Enter' && e.target.tagName !== 'BUTTON') { e.preventDefault(); saveBtn.click(); }
    if (e.key === 'Escape') close();
  };

  modal.style.display = 'flex';
  refresh();
  setTimeout(() => dateEl.focus(), 40);
}

// --- Structured Completion / Check-in Dialog (Duration + Date boxes & Quick Chips) ---
function openCheckinDialog(opts) {
  const modal = document.getElementById('modal-checkin');
  if (!modal) return;
  const $ = id => document.getElementById(id);
  const dateEl = $('ci-date'), minEl = $('ci-minutes'), noteEl = $('ci-note');
  const dateGroup = $('ci-date-group'), noteGroup = $('ci-note-group');
  const planned = Math.max(5, parseInt(opts.plannedMinutes, 10) || 30);

  $('ci-title').textContent = opts.title || 'Log Completion';
  $('ci-item-name').textContent = opts.itemName || '';
  $('ci-planned-label').textContent = `Planned duration: ${planned} min`;
  $('ci-btn-planned').textContent = `Planned (${planned}m)`;

  dateGroup.style.display = opts.showDate ? 'block' : 'none';
  noteGroup.style.display = opts.showNote !== false ? 'block' : 'none';
  dateEl.value = opts.date || fmtDate(new Date());
  minEl.value = planned;
  noteEl.value = '';

  const refresh = () => {
    const mins = Math.max(1, parseInt(minEl.value, 10) || planned);
    const ratio = (mins / planned).toFixed(2);
    $('ci-ratio-label').textContent = `${mins} min (${ratio}× planned)`;
  };

  modal.querySelectorAll('[data-ci-delta]').forEach(btn => {
    btn.onclick = () => {
      const cur = parseInt(minEl.value, 10) || planned;
      minEl.value = Math.max(5, cur + parseInt(btn.dataset.ciDelta, 10));
      refresh();
    };
  });
  $('ci-btn-planned').onclick = () => {
    minEl.value = planned;
    refresh();
  };
  minEl.oninput = refresh;
  minEl.onchange = refresh;

  let finished = false;
  const close = (cancelled = false) => {
    modal.style.display = 'none';
    ['ci-close', 'ci-cancel', 'ci-save'].forEach(id => { $(id).onclick = null; });
    modal.onkeydown = null;
    if (cancelled && !finished && typeof opts.onCancel === 'function') {
      opts.onCancel();
    }
  };

  $('ci-close').onclick = () => close(true);
  $('ci-cancel').onclick = () => close(true);
  $('ci-save').onclick = async () => {
    const mins = Math.max(1, parseInt(minEl.value, 10) || planned);
    const dateStr = dateEl.value || fmtDate(new Date());
    const note = noteEl.value.trim();
    finished = true;
    close(false);
    if (typeof opts.onSave === 'function') {
      await opts.onSave({ minutes: mins, date: dateStr, note });
    }
  };
  modal.onkeydown = e => {
    if (e.key === 'Enter' && e.target.tagName !== 'BUTTON') { e.preventDefault(); $('ci-save').click(); }
    if (e.key === 'Escape') close(true);
  };

  modal.style.display = 'flex';
  refresh();
  setTimeout(() => minEl.focus(), 40);
}

// --- Habits View ---
let isHabitChatBuffering = false;

function initHabitsView() {
  const chatInput = document.getElementById('habits-chat-input');
  const btnSend = document.getElementById('btn-send-habit-chat');

  btnSend.addEventListener('click', () => {
    const text = chatInput.value.trim();
    if (!text || isHabitChatBuffering) return;
    sendHabitChat(text);
  });

  chatInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !isHabitChatBuffering) btnSend.click();
  });

  // Scrollable transcript: "jump to latest" pill appears when scrolled away from the bottom
  const msgs = document.getElementById('habits-chat-messages');
  const jump = document.getElementById('habits-chat-jump');
  const nearBottom = () => msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 48;
  msgs.addEventListener('scroll', () => jump.classList.toggle('visible', !nearBottom()));
  jump.addEventListener('click', () => msgs.scrollTo({ top: msgs.scrollHeight, behavior: 'smooth' }));
  new ResizeObserver(() => jump.classList.toggle('visible', !nearBottom())).observe(msgs);

  document.querySelectorAll('.quick-chips .chip').forEach(c => {
    c.addEventListener('click', () => {
      if (isHabitChatBuffering) return;
      sendHabitChat(c.dataset.msg);
    });
  });

  document.getElementById('btn-resync-habits').addEventListener('click', async () => {
    try {
      showToast('Syncing habits with Apple Calendar...', 'info');
      const res = await fetch('/api/habits/resync', { method: 'POST' });
      const data = await res.json();
      if (data.ok) {
        showToast(`Habits resynced: ${data.resync.recreated} recreated, ${data.resync.imported} imported`, 'success');
        loadState();
      }
    } catch (err) {
      showToast('Resync failed: ' + err, 'error');
    }
  });

  // Modal new / edit habit
  const modal = document.getElementById('modal-habit');
  document.getElementById('btn-new-habit').addEventListener('click', () => openHabitModal(null));
  document.getElementById('modal-habit-close').addEventListener('click', () => modal.style.display = 'none');
  document.getElementById('modal-habit-cancel').addEventListener('click', () => modal.style.display = 'none');

  document.querySelectorAll('#m-habit-days .day-btn').forEach(b => {
    b.addEventListener('click', () => b.classList.toggle('active'));
  });

  document.querySelectorAll('#m-habit-quick-dur [data-habit-dur]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.getElementById('m-habit-duration').value = btn.dataset.habitDur;
    });
  });

  document.getElementById('modal-habit-save').addEventListener('click', async () => {
    const name = document.getElementById('m-habit-name').value.trim();
    if (!name) return showToast('Habit name is required', 'warning');
    const days = [];
    document.querySelectorAll('#m-habit-days .day-btn.active').forEach(b => {
      days.push(parseInt(b.dataset.day, 10));
    });
    if (days.length === 0) return showToast('Select at least one day of the week', 'warning');
    const start = document.getElementById('m-habit-start').value || '08:00';
    const minutes = parseInt(document.getElementById('m-habit-duration').value, 10) || 45;
    const notes = document.getElementById('m-habit-notes').value;
    const editId = modal.dataset.editHabitId ? parseInt(modal.dataset.editHabitId, 10) : null;

    try {
      const url = editId ? `/api/habits/${editId}/update` : '/api/habits';
      await api(url, {
        method: 'POST',
        body: { name, days, start, minutes, notes }
      });
      modal.style.display = 'none';
      showToast(editId ? `Habit '${name}' updated` : `Habit '${name}' created and added to Calendar`, 'success');
      loadState();
    } catch (err) {
      showToast('Error: ' + err.message, 'error');
    }
  });
}

function openHabitModal(habit = null) {
  const modal = document.getElementById('modal-habit');
  if (!modal) return;
  const isEdit = !!(habit && habit.id);
  modal.dataset.editHabitId = isEdit ? String(habit.id) : '';
  document.getElementById('modal-habit-title').textContent = isEdit ? `Edit Habit · ${habit.name}` : 'Add Recurring Habit';
  document.getElementById('modal-habit-save').textContent = isEdit ? 'Save Changes' : 'Save & Create Recurring Event';

  document.getElementById('m-habit-name').value = isEdit ? (habit.name || '') : '';
  document.getElementById('m-habit-start').value = isEdit ? (habit.start || '08:00') : '08:00';
  document.getElementById('m-habit-duration').value = isEdit ? (habit.minutes || 45) : 45;
  document.getElementById('m-habit-notes').value = isEdit ? (habit.notes || '') : '';

  const activeDays = isEdit ? (habit.days || []) : [0, 1, 2, 3, 4];
  document.querySelectorAll('#m-habit-days .day-btn').forEach(b => {
    const d = parseInt(b.dataset.day, 10);
    b.classList.toggle('active', activeDays.includes(d));
  });

  modal.style.display = 'flex';
  setTimeout(() => document.getElementById('m-habit-name').focus(), 40);
}

async function sendHabitChat(text) {
  if (isHabitChatBuffering) return;
  const container = document.getElementById('habits-chat-messages');
  const chatInput = document.getElementById('habits-chat-input');
  const btnSend = document.getElementById('btn-send-habit-chat');

  isHabitChatBuffering = true;

  // 1. Append user bubble
  const userBubble = document.createElement('div');
  userBubble.className = 'chat-bubble chat-user';
  userBubble.textContent = text;
  container.appendChild(userBubble);
  chatInput.value = '';

  // 2. Append animated thinking / buffering bubble
  const thinkingBubble = document.createElement('div');
  thinkingBubble.className = 'chat-bubble chat-agent chat-thinking';
  thinkingBubble.id = 'habits-thinking-bubble';
  thinkingBubble.innerHTML = `
    <div class="thinking-dots">
      <span class="dot"></span>
      <span class="dot"></span>
      <span class="dot"></span>
    </div>
    <span class="thinking-label">Agent is thinking...</span>
  `;
  container.appendChild(thinkingBubble);
  container.scrollTop = container.scrollHeight;

  // 3. Update input & button state
  btnSend.disabled = true;
  btnSend.innerHTML = '<span class="btn-spinner"></span> <span>...</span>';
  chatInput.disabled = true;
  chatInput.placeholder = 'Agent is thinking...';

  try {
    const res = await fetch('/api/habits/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text })
    });
    const data = await res.json();

    // Remove thinking bubble
    if (thinkingBubble.parentNode) {
      thinkingBubble.parentNode.removeChild(thinkingBubble);
    }

    if (!res.ok || data.error) {
      const errBubble = document.createElement('div');
      errBubble.className = 'chat-bubble chat-agent';
      errBubble.style.borderColor = 'var(--danger)';
      errBubble.innerHTML = `<strong>Error:</strong> ${escapeHtml(data.error || 'Failed to get agent response')}`;
      container.appendChild(errBubble);
      showToast(data.error || 'Agent call failed', 'error');
    } else {
      const agentBubble = document.createElement('div');
      agentBubble.className = 'chat-bubble chat-agent';
      let html = escapeHtml(data.reply || '').replace(/\n/g, '<br>');
      if (data.actions_taken && data.actions_taken.length > 0) {
        html += '<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px">' +
          data.actions_taken.map(a => `<span class="chat-action-badge">${escapeHtml(a)}</span>`).join('') +
          '</div>';
      }
      agentBubble.innerHTML = html;
      container.appendChild(agentBubble);
      // Long answers: start reading from their top instead of jumping to the end
      if (agentBubble.offsetHeight > container.clientHeight * 0.6) {
        container.scrollTop = agentBubble.offsetTop - container.offsetTop - 8;
      }
      loadState();
    }
  } catch (err) {
    if (thinkingBubble.parentNode) {
      thinkingBubble.parentNode.removeChild(thinkingBubble);
    }
    const errBubble = document.createElement('div');
    errBubble.className = 'chat-bubble chat-agent';
    errBubble.style.borderColor = 'var(--danger)';
    errBubble.innerHTML = `<strong>Connection error:</strong> ${escapeHtml(String(err))}`;
    container.appendChild(errBubble);
    showToast('Habit chat failed: ' + err, 'error');
  } finally {
    isHabitChatBuffering = false;
    btnSend.disabled = false;
    btnSend.textContent = 'Send';
    chatInput.disabled = false;
    chatInput.placeholder = 'Talk with habit agent...';
    chatInput.focus();
    const last = container.lastElementChild;
    if (!(last && last.classList.contains('chat-agent') && last.offsetHeight > container.clientHeight * 0.6)) {
      container.scrollTop = container.scrollHeight;
    }
  }
}

function renderHabitsList() {
  const container = document.getElementById('habits-list');
  container.innerHTML = '';
  const habits = S.habits || [];

  if (habits.length === 0) {
    container.innerHTML = '<div style="color:var(--text-dim);padding:14px;text-align:center">No active habits yet. Add one above!</div>';
    return;
  }

  const dayLetters = ['M', 'T', 'W', 'T', 'F', 'S', 'S'];

  habits.forEach(h => {
    const item = document.createElement('div');
    item.className = 'habit-item';
    const days = h.days || [];

    item.innerHTML = `
      <div>
        <div class="habit-name">${escapeHtml(h.name)}</div>
        <div class="habit-meta">
          <span>${escapeHtml(h.start)} · ${h.minutes} min</span>
          <div class="habit-days">
            ${dayLetters.map((l, idx) => `<span class="day-badge ${days.includes(idx) ? 'active' : ''}">${l}</span>`).join('')}
          </div>
        </div>
      </div>
      <div style="display:flex;gap:6px;align-items:center">
        <button class="btn btn-sm btn-secondary btn-checkin">✓ Done</button>
        <button class="btn btn-sm btn-icon btn-rename-habit" title="Rename Habit">✎</button>
        <button class="btn btn-sm btn-icon btn-del-habit" title="Delete habit and remove series from calendar">✕</button>
      </div>
    `;

    item.querySelector('.btn-checkin').addEventListener('click', () => {
      openCheckinDialog({
        title: 'Habit Check-in',
        itemName: h.name,
        plannedMinutes: h.minutes || 45,
        showDate: true,
        showNote: true,
        onSave: async ({ minutes, date, note }) => {
          try {
            await api(`/api/habits/${h.id}/checkin`, {
              method: 'POST',
              body: { status: 'done', actual_minutes: minutes, date, note }
            });
            showToast(`Habit '${h.name}' check-in saved`, 'success');
            loadState();
          } catch (e) {
            showToast('Check-in failed: ' + e.message, 'error');
          }
        }
      });
    });

    const editBtn = item.querySelector('.btn-rename-habit');
    editBtn.title = 'Edit Habit (Time, Days, Duration, Name)';
    editBtn.addEventListener('click', () => openHabitModal(h));

    item.querySelector('.btn-del-habit').addEventListener('click', async () => {
      const ok = await uiConfirm(`Permanently delete habit '${h.name}' and wipe its recurring events from Apple Calendar?`, 'Delete Habit');
      if (ok) {
        try {
          await api(`/api/habits/${h.id}`, { method: 'DELETE' });
          showToast(`Habit '${h.name}' deleted and wiped from calendar`, 'info');
          loadState();
        } catch (e) {
          showToast('Failed to delete habit: ' + e.message, 'error');
        }
      }
    });

    container.appendChild(item);
  });
}

function renderHabitLogs() {
  const container = document.getElementById('habit-logs-list');
  container.innerHTML = '';
  const logs = S.habit_logs || [];

  if (logs.length === 0) {
    container.innerHTML = '<span style="font-size:11px;color:var(--text-dim)">No recent check-ins recorded</span>';
    return;
  }

  logs.slice(0, 8).forEach(l => {
    const h = (S.habits || []).find(x => x.id === l.habit_id) || { name: 'Habit' };
    const el = document.createElement('div');
    el.style.fontSize = '12px';
    el.style.padding = '4px 0';
    el.style.borderBottom = '1px solid var(--border)';
    el.innerHTML = `<strong>${escapeHtml(l.date)}</strong>: ${escapeHtml(h.name)} — <span style="color:var(--accent)">${l.actual_minutes || l.planned_minutes}m</span> (${l.status})`;
    container.appendChild(el);
  });
}

// --- Proposals Inbox ---
function initProposalsView() {}

function renderProposalsList() {
  const container = document.getElementById('proposals-list');
  container.innerHTML = '';
  const proposals = (S.proposals || []).filter(p => p.status === 'pending');

  if (proposals.length === 0) {
    container.innerHTML = '<div style="color:var(--text-dim);padding:20px;text-align:center">Inbox is empty — all calendar events and planned blocks are in harmony!</div>';
    return;
  }

  proposals.forEach(p => {
    const item = document.createElement('div');
    item.className = 'proposal-item';
    item.innerHTML = `
      <div class="proposal-info">
        <h3>${escapeHtml(p.title)}</h3>
        <p>${p.rationale}</p>
        <div style="font-size:11px;color:var(--text-dim);margin-top:6px">${p.actions.map(a => `${a.type} -> block #${a.block_id} to ${a.new_start || ''}`).join('; ')}</div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn btn-sm btn-primary btn-apply">Approve & Apply</button>
        <button class="btn btn-sm btn-secondary btn-dismiss">Dismiss</button>
      </div>
    `;

    item.querySelector('.btn-apply').addEventListener('click', async () => {
      try {
        await api(`/api/proposals/${p.id}/apply`, { method: 'POST' });
        showToast('Proposal applied', 'success');
      } catch (e) { showToast('Could not apply: ' + e.message, 'error'); }
      loadState();
    });

    item.querySelector('.btn-dismiss').addEventListener('click', async () => {
      try {
        await api(`/api/proposals/${p.id}/reject`, { method: 'POST' });
      } catch (e) { showToast('Could not dismiss: ' + e.message, 'error'); }
      loadState();
    });

    container.appendChild(item);
  });
}

// --- Memory & Calibration View ---
function initMemoryView() {
  document.getElementById('btn-add-belief').addEventListener('click', () => openBeliefDialog(null));
}

function openBeliefDialog(belief = null) {
  const modal = document.getElementById('modal-belief');
  if (!modal) return;
  const $ = id => document.getElementById(id);
  const scopeEl = $('m-belief-scope');
  const textEl = $('m-belief-text');
  const isEdit = !!(belief && belief.id);

  $('m-belief-title').textContent = isEdit ? 'Edit Agent Belief' : 'Add Agent Belief';
  scopeEl.innerHTML = `
    <option value="global">Global (All planning &amp; scheduling)</option>
    <option value="habits">Habits &amp; Routines</option>
  `;
  (S.categories || []).forEach(c => {
    const opt = document.createElement('option');
    opt.value = c.id;
    opt.textContent = `${c.label} (${c.kind === 'course' ? 'Subfolder' : 'Project Folder'})`;
    scopeEl.appendChild(opt);
  });
  scopeEl.value = (belief && belief.scope) || 'global';
  if (belief && belief.scope && !Array.from(scopeEl.options).some(o => o.value === belief.scope)) {
    const opt = document.createElement('option');
    opt.value = belief.scope;
    opt.textContent = belief.scope;
    scopeEl.appendChild(opt);
    scopeEl.value = belief.scope;
  }
  textEl.value = (belief && belief.text) || '';

  const close = () => {
    modal.style.display = 'none';
    ['m-belief-close', 'm-belief-cancel', 'm-belief-save'].forEach(id => { $(id).onclick = null; });
    modal.onkeydown = null;
  };

  $('m-belief-close').onclick = close;
  $('m-belief-cancel').onclick = close;
  $('m-belief-save').onclick = async () => {
    const text = textEl.value.trim();
    const scope = scopeEl.value || 'global';
    if (!text) return showToast('Belief text is required', 'warning');
    try {
      await api('/api/beliefs', {
        method: 'POST',
        body: { id: isEdit ? belief.id : undefined, text, scope }
      });
      showToast(isEdit ? 'Belief updated' : 'Belief remembered', 'success');
      close();
      loadState();
    } catch (e) {
      showToast('Could not save belief: ' + e.message, 'error');
    }
  };
  modal.onkeydown = e => {
    if (e.key === 'Enter' && e.target.tagName !== 'BUTTON') { e.preventDefault(); $('m-belief-save').click(); }
    if (e.key === 'Escape') close();
  };

  modal.style.display = 'flex';
  setTimeout(() => textEl.focus(), 40);
}

async function renderMemoryView() {
  // Fetch detailed calibration stats
  try {
    const res = await fetch('/api/calibration');
    if (!res.ok) return;
    const cal = await res.json();

    const stats = document.getElementById('calibration-stats');
    const firstCat = (S.categories && S.categories[0]) ? S.categories[0] : { id: 'thesis', label: 'Primary Folder' };
    const firstCatRatio = ((cal.categories || {})[firstCat.id] || {}).ratio || 1.0;
    stats.innerHTML = `
      <div class="cal-stat-box">
        <div class="cal-stat-val">${cal.overall.ratio}×</div>
        <div class="cal-stat-lbl">Overall Multiplier</div>
      </div>
      <div class="cal-stat-box">
        <div class="cal-stat-val">${cal.overall.n}</div>
        <div class="cal-stat-lbl">Completed Tasks</div>
      </div>
      <div class="cal-stat-box">
        <div class="cal-stat-val">${firstCatRatio}×</div>
        <div class="cal-stat-lbl">${escapeHtml(firstCat.label)} Multiplier</div>
      </div>
    `;

    const tbody = document.querySelector('#samples-table tbody');
    tbody.innerHTML = '';
    (cal.recent_samples || []).forEach(s => {
      const tr = document.createElement('tr');
      const ratio = (s.actual / s.estimated).toFixed(2);
      tr.innerHTML = `
        <td>${escapeHtml(s.label)}</td>
        <td>${escapeHtml(s.scope)}</td>
        <td>${s.estimated}m</td>
        <td>${s.actual}m</td>
        <td style="font-weight:600;color:${ratio > 1 ? 'var(--danger-text)' : 'var(--success-text)'}">${ratio}×</td>
        <td style="text-align:right;white-space:nowrap">
          <button type="button" class="btn btn-sm btn-icon btn-edit-sample" title="Edit actual duration">✎</button>
          <button type="button" class="btn btn-sm btn-icon btn-del-sample" title="Delete calibration sample">✕</button>
        </td>
      `;
      tr.querySelector('.btn-edit-sample').addEventListener('click', () => {
        openCheckinDialog({
          title: 'Edit Calibration Sample',
          itemName: `${s.label} (est. ${s.estimated}m)`,
          plannedMinutes: s.actual || s.estimated || 30,
          showDate: false,
          showNote: false,
          onSave: async ({ minutes }) => {
            try {
              await api(`/api/samples/${s.id}/update`, {
                method: 'POST',
                body: { actual: minutes }
              });
              showToast(`Updated sample '${s.label}' to ${minutes}m`, 'success');
              await renderMemoryView();
              loadState();
            } catch (err) {
              showToast('Could not update sample: ' + err.message, 'error');
            }
          }
        });
      });
      tr.querySelector('.btn-del-sample').addEventListener('click', async () => {
        try {
          await api(`/api/samples/${s.id}/delete`, { method: 'POST', body: {} });
          tr.remove();
          showToast(`Deleted calibration sample '${s.label}'`, 'info');
          await renderMemoryView();
          loadState();
        } catch (err) {
          showToast('Could not delete sample: ' + err.message, 'error');
        }
      });
      tbody.appendChild(tr);
    });
  } catch (err) {
    console.error('renderMemoryView error:', err);
  }

  // Beliefs
  const bList = document.getElementById('beliefs-list');
  bList.innerHTML = '';
  (S.beliefs || []).forEach(b => {
    const item = document.createElement('div');
    item.className = 'belief-item';
    item.innerHTML = `
      <div><span class="belief-scope">[${escapeHtml(b.scope)}]</span> ${escapeHtml(b.text)}</div>
      <div style="display:flex;gap:4px">
        <button type="button" class="btn btn-sm btn-icon btn-edit-belief" title="Edit Belief">✎</button>
        <button type="button" class="btn btn-sm btn-icon btn-del-belief" title="Delete Belief">✕</button>
      </div>
    `;
    item.querySelector('.btn-edit-belief').addEventListener('click', () => openBeliefDialog(b));
    item.querySelector('.btn-del-belief').addEventListener('click', async () => {
      try {
        await api(`/api/beliefs/${b.id}`, { method: 'DELETE' });
        item.remove();
        showToast('Belief deleted', 'info');
        loadState();
      } catch (err) {
        showToast('Could not delete belief: ' + err.message, 'error');
      }
    });
    bList.appendChild(item);
  });
}

function initLLMSettings() {
  const pill = document.getElementById('ai-status');
  const modal = document.getElementById('modal-llm');
  const btnClose = document.getElementById('modal-llm-close');
  const btnCancel = document.getElementById('modal-llm-cancel');
  const btnSave = document.getElementById('modal-llm-save');
  const selProv = document.getElementById('m-llm-provider');
  const inpModel = document.getElementById('m-llm-model');
  const inpUrl = document.getElementById('m-llm-url');
  const inpKey = document.getElementById('m-llm-key');
  const urlGroup = document.getElementById('m-llm-url-group');
  const helpText = document.getElementById('m-llm-model-help');
  const testRes = document.getElementById('m-llm-test-result');

  if (!pill || !modal) return;

  async function openModal() {
    testRes.style.display = 'none';
    inpKey.value = '';
    try {
      const res = await fetch('/api/config/llm');
      if (res.ok) {
        const data = await res.json();
        selProv.value = data.provider || 'gemini';
        inpModel.value = data.model || 'gemini-3.5-flash';
        inpUrl.value = data.base_url || '';
        if (data.has_key) {
          inpKey.placeholder = 'API Key is already configured (leave blank to keep)';
        } else {
          inpKey.placeholder = 'Enter API Key (starts with AIzaSy... for Gemini)';
        }
        updateFormVisibility();
      }
    } catch (e) {
      console.error(e);
    }
    modal.style.display = 'flex';
  }

  function updateFormVisibility() {
    const prov = selProv.value;
    if (prov === 'gemini') {
      urlGroup.style.display = 'none';
      helpText.innerHTML = 'Recommended: <code>gemini-3.5-flash</code> (fast & stable) or <code>gemini-3.8-flash</code> (1,500 req/day free)';
      if (!inpModel.value || inpModel.value.includes('openrouter')) {
        inpModel.value = 'gemini-3.5-flash';
      }
    } else if (prov === 'openrouter') {
      urlGroup.style.display = 'none';
      helpText.innerHTML = 'OpenRouter Free: <code>openrouter/free</code> (50 req/day free tier)';
      if (!inpModel.value || inpModel.value.includes('gemini')) {
        inpModel.value = 'openrouter/free';
      }
    } else {
      urlGroup.style.display = 'block';
      helpText.textContent = 'Custom OpenAI-compatible model and endpoint URL';
    }
  }

  selProv.addEventListener('change', updateFormVisibility);
  pill.addEventListener('click', openModal);
  btnClose.addEventListener('click', () => { modal.style.display = 'none'; });
  btnCancel.addEventListener('click', () => { modal.style.display = 'none'; });

  btnSave.addEventListener('click', async () => {
    const provider = selProv.value;
    const model = inpModel.value.trim();
    const base_url = inpUrl.value.trim();
    const key = inpKey.value.trim();

    btnSave.disabled = true;
    btnSave.textContent = 'Testing connection...';
    testRes.style.display = 'block';
    testRes.style.background = 'var(--bg-subtle)';
    testRes.style.color = 'var(--text-muted)';
    testRes.textContent = 'Verifying connection to model...';

    try {
      const res = await fetch('/api/config/llm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider, model, base_url, key, test: true })
      });
      const data = await res.json();
      if (data.ok) {
        if (data.test && data.test.ok) {
          testRes.style.background = 'rgba(46, 160, 67, 0.15)';
          testRes.style.color = 'var(--success)';
          testRes.textContent = `Connected! ${data.test.model} responded: "${data.test.reply}"`;
          showToast(`LLM configured: ${data.test.model}`, 'success');
          setTimeout(() => {
            modal.style.display = 'none';
            loadState();
          }, 1200);
        } else {
          const err = data.test ? data.test.error : 'Connection test failed';
          testRes.style.background = 'rgba(207, 34, 46, 0.15)';
          testRes.style.color = 'var(--danger)';
          testRes.textContent = `Error: ${err}`;
          loadState();
        }
      } else {
        testRes.style.background = 'rgba(207, 34, 46, 0.15)';
        testRes.style.color = 'var(--danger)';
        testRes.textContent = data.error || 'Failed to save configuration';
      }
    } catch (e) {
      testRes.style.background = 'rgba(207, 34, 46, 0.15)';
      testRes.style.color = 'var(--danger)';
      testRes.textContent = `Request failed: ${e.message}`;
    } finally {
      btnSave.disabled = false;
      btnSave.textContent = 'Save & Test Connection';
    }
  });
}

function initCalendarAuth() {
  const btn = document.getElementById('btn-request-cal');
  if (btn) {
    btn.addEventListener('click', async () => {
      showToast('Requesting Full Calendar Access from macOS...', 'info');
      await fetch('/api/calendar/request', { method: 'POST' });
      loadState();
    });
  }
}

let wsDraftSources = [];

function initWorkspaceSettings() {
  const modal = document.getElementById('modal-workspace');
  if (!modal) return;

  ['workspace-status', 'cal-status', 'btn-manage-folders', 'btn-sidebar-workspace'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('click', () => openWorkspaceModal());
  });

  document.getElementById('ws-close').addEventListener('click', () => { modal.style.display = 'none'; });
  document.getElementById('ws-cancel').addEventListener('click', () => { modal.style.display = 'none'; });

  document.querySelectorAll('#ws-work-days .day-btn').forEach(b => {
    b.addEventListener('click', () => b.classList.toggle('active'));
  });

  document.getElementById('ws-btn-add-source').addEventListener('click', () => {
    const defaultCal = document.getElementById('ws-cal-thesis').value.trim() || 'Thesis';
    wsDraftSources.push({
      id: 'folder-' + (wsDraftSources.length + 1),
      label: 'New Folder',
      path: '',
      mode: 'single',
      calendar: defaultCal,
      exclude: [],
      enabled: true,
      exists: false,
      item_count: 0,
      subfolders: []
    });
    renderWsSourcesList();
  });

  document.getElementById('ws-btn-pick-new').addEventListener('click', async () => {
    const btn = document.getElementById('ws-btn-pick-new');
    btn.disabled = true;
    btn.textContent = 'Opening Finder…';
    try {
      const res = await api('/api/config/pick_folder', {
        method: 'POST',
        body: { prompt: 'Select a project, course, or workspace folder for Planner file analysis:' }
      });
      if (res && res.ok && res.path) {
        const hasManySubfolders = (res.subfolders || []).length >= 2;
        const defaultCal = hasManySubfolders
          ? (document.getElementById('ws-cal-uni').value.trim() || 'University')
          : (document.getElementById('ws-cal-thesis').value.trim() || 'Thesis');
        wsDraftSources.push({
          id: '',
          label: res.suggested_label || 'Workspace',
          path: res.path,
          mode: hasManySubfolders ? 'subfolders' : 'single',
          calendar: defaultCal,
          exclude: [],
          enabled: true,
          exists: true,
          item_count: hasManySubfolders ? (res.subfolders || []).length : (res.item_count || 0),
          subfolders: res.subfolders || []
        });
        renderWsSourcesList();
        showToast(`Added folder: ${res.path}`, 'success');
      }
    } catch (e) {
      showToast('Could not open folder picker: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = '📁 Pick Folder in Finder…';
    }
  });

  document.getElementById('ws-cals-all').addEventListener('click', () => {
    document.querySelectorAll('#ws-inherited-cals input[type="checkbox"]').forEach(cb => { cb.checked = true; });
  });
  document.getElementById('ws-cals-none').addEventListener('click', () => {
    document.querySelectorAll('#ws-inherited-cals input[type="checkbox"]').forEach(cb => { cb.checked = false; });
  });

  document.getElementById('ws-save').addEventListener('click', async () => {
    const btn = document.getElementById('ws-save');
    btn.disabled = true;
    btn.textContent = 'Saving…';
    try {
      const calThesis = document.getElementById('ws-cal-thesis').value.trim() || 'Thesis';
      const calUni = document.getElementById('ws-cal-uni').value.trim() || 'University';
      const calHabits = document.getElementById('ws-cal-habits').value.trim() || 'Habits';

      const ignoreList = [];
      const readAlsoList = [];
      document.querySelectorAll('#ws-inherited-cals input[type="checkbox"]').forEach(cb => {
        const title = cb.dataset.calTitle;
        if (!title) return;
        if (cb.checked) {
          readAlsoList.push(title);
        } else {
          ignoreList.push(title);
        }
      });

      const workDays = [];
      document.querySelectorAll('#ws-work-days .day-btn.active').forEach(b => {
        workDays.push(parseInt(b.dataset.wday, 10));
      });

      const payload = {
        sources: wsDraftSources,
        calendars: {
          thesis: calThesis,
          university: calUni,
          habits: calHabits,
          ignore: ignoreList,
          read_also: readAlsoList
        },
        work: {
          days: workDays.length ? workDays : [0, 1, 2, 3, 4],
          day_start: document.getElementById('ws-day-start').value || '09:00',
          day_end: document.getElementById('ws-day-end').value || '19:00',
          max_block_min: parseInt(document.getElementById('ws-max-block').value, 10) || 90,
          max_day_min: parseInt(document.getElementById('ws-max-day').value, 10) || 360,
          weekend_if_needed: document.getElementById('ws-weekend-needed').checked
        }
      };

      await api('/api/config/workspace', {
        method: 'POST',
        body: payload
      });
      showToast('Workspace folders, calendars, and schedule saved!', 'success');
      modal.style.display = 'none';
      loadState();
    } catch (e) {
      showToast('Failed to save workspace settings: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Save Workspace Settings';
    }
  });
}

async function openWorkspaceModal() {
  const modal = document.getElementById('modal-workspace');
  if (!modal) return;

  try {
    const data = await api('/api/config/workspace');
    wsDraftSources = (data.sources || []).map(s => ({ ...s }));
    const calsCfg = data.calendars || {};
    const calsList = data.calendars_list || S.calendars_list || [];
    const workCfg = data.work || {};

    // Populate writable calendars datalist
    const dl = document.getElementById('ws-cal-datalist');
    dl.innerHTML = '';
    calsList.filter(c => c.writable !== false).forEach(c => {
      const cName = (c.name || c.title || '').trim();
      if (!cName) return;
      const opt = document.createElement('option');
      opt.value = cName;
      opt.textContent = `${cName} (${c.source || 'Local'})`;
      dl.appendChild(opt);
    });

    document.getElementById('ws-cal-thesis').value = calsCfg.thesis || 'Thesis';
    document.getElementById('ws-cal-uni').value = calsCfg.university || 'University';
    document.getElementById('ws-cal-habits').value = calsCfg.habits || 'Habits';

    renderWsSourcesList();

    // Populate inherited calendars checkboxes
    const inhContainer = document.getElementById('ws-inherited-cals');
    inhContainer.innerHTML = '';
    const ownedSet = new Set([calsCfg.thesis, calsCfg.university, calsCfg.habits].map(x => (x || '').trim().toLowerCase()));
    const ignoreSet = new Set((calsCfg.ignore || []).map(x => (x || '').trim().toLowerCase()));
    const externalCals = calsList.filter(c => {
      const cName = (c.name || c.title || '').trim();
      return cName && !ownedSet.has(cName.toLowerCase());
    });

    if (externalCals.length === 0) {
      inhContainer.innerHTML = '<span style="font-size:11px;color:var(--text-dim)">No additional calendars found in Apple Calendar.</span>';
    } else {
      externalCals.forEach(c => {
        const cName = (c.name || c.title || '').trim();
        const rawSrc = (c.source || 'Local').trim();
        const srcShort = rawSrc.includes('@') ? 'Uni/Exch' : rawSrc.replace('Subscribed Calendars', 'Sub');
        const isEnabled = !ignoreSet.has(cName.toLowerCase());
        const lbl = document.createElement('label');
        lbl.style.cssText = 'display:flex;align-items:center;gap:7px;font-size:12px;cursor:pointer;overflow:hidden;padding:4px 6px;border-radius:4px;';
        lbl.title = `${cName} (${rawSrc})`;
        lbl.innerHTML = `
          <input type="checkbox" data-cal-title="${escapeHtml(cName)}" ${isEnabled ? 'checked' : ''} style="flex-shrink:0">
          <span style="flex:1;min-width:0;font-weight:500;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml(cName)}</span>
          <span class="cal-source-badge" style="margin-left:auto;flex-shrink:0">${escapeHtml(srcShort)}</span>
        `;
        inhContainer.appendChild(lbl);
      });
    }

    // Populate working hours
    const activeDays = workCfg.days || [0, 1, 2, 3, 4];
    document.querySelectorAll('#ws-work-days .day-btn').forEach(b => {
      b.classList.toggle('active', activeDays.includes(parseInt(b.dataset.wday, 10)));
    });
    document.getElementById('ws-day-start').value = workCfg.day_start || '09:00';
    document.getElementById('ws-day-end').value = workCfg.day_end || '19:00';
    document.getElementById('ws-max-block').value = workCfg.max_block_min || 90;
    document.getElementById('ws-max-day').value = workCfg.max_day_min || 360;
    document.getElementById('ws-weekend-needed').checked = workCfg.weekend_if_needed !== false;
  } catch (e) {
    showToast('Failed to load workspace settings: ' + e.message, 'error');
  }

  modal.style.display = 'flex';
}

function renderWsSourcesList() {
  const container = document.getElementById('ws-sources-list');
  if (!container) return;
  container.innerHTML = '';

  if (wsDraftSources.length === 0) {
    container.innerHTML = '<div style="padding:14px;text-align:center;color:var(--text-dim);font-size:12px">No folders configured. Click <strong>📁 Pick Folder in Finder…</strong> above to add one!</div>';
    return;
  }

  wsDraftSources.forEach((src, idx) => {
    const card = document.createElement('div');
    card.className = 'ws-source-card';
    const exclStr = Array.isArray(src.exclude) ? src.exclude.join(', ') : (src.exclude || '');
    const statusColor = src.exists ? 'var(--success-text)' : 'var(--warning-text)';
    const statusText = src.exists
      ? (src.mode === 'subfolders'
          ? `✓ Found (${src.item_count || 0} subfolder categories${(src.subfolders && src.subfolders.length) ? ': ' + src.subfolders.slice(0, 6).join(', ') : ''})`
          : `✓ Found (${src.item_count || 0} top-level files/folders)`)
      : (src.path ? '⚠ Folder not found on disk' : 'Choose or enter a folder path below');

    card.innerHTML = `
      <div class="ws-source-top">
        <div class="form-group" style="align-items:center">
          <label title="Enable or disable this folder">Active</label>
          <input type="checkbox" class="ws-src-enabled" ${src.enabled !== false ? 'checked' : ''} style="width:16px;height:16px;margin-top:6px">
        </div>
        <div class="form-group">
          <label>Label</label>
          <input type="text" class="input-text ws-src-label" value="${escapeHtml(src.label || '')}" placeholder="e.g. Thesis, Courses, Work">
        </div>
        <div class="form-group">
          <label>Analysis Mode</label>
          <select class="select ws-src-mode">
            <option value="single" ${src.mode !== 'subfolders' ? 'selected' : ''}>Single Project / Repo Folder</option>
            <option value="subfolders" ${src.mode === 'subfolders' ? 'selected' : ''}>Subfolders = Separate Categories (Courses / Projects)</option>
          </select>
        </div>
        <div class="form-group">
          <label>Target Calendar</label>
          <input type="text" list="ws-cal-datalist" class="input-text ws-src-cal" value="${escapeHtml(src.calendar || '')}" placeholder="e.g. Thesis">
        </div>
        <button type="button" class="btn btn-sm btn-icon ws-src-del" title="Remove this folder source" style="margin-bottom:2px">✕</button>
      </div>
      <div class="ws-source-path-row">
        <input type="text" class="input-text ws-src-path flex-1" value="${escapeHtml(src.path || '')}" placeholder="~/Desktop/my-project or /Users/..." style="font-family:var(--mono);font-size:12px">
        <button type="button" class="btn btn-sm btn-secondary ws-src-browse" style="white-space:nowrap">📁 Browse…</button>
      </div>
      <div class="ws-src-excl-row" style="display:${src.mode === 'subfolders' ? 'flex' : 'none'};gap:8px;align-items:center">
        <label style="font-size:11px;color:var(--text-muted);white-space:nowrap">Ignore subfolders (comma-separated):</label>
        <input type="text" class="input-text ws-src-excl flex-1" value="${escapeHtml(exclStr)}" placeholder="e.g. Archive, OeAD, .git" style="font-size:11.5px;padding:4px 8px">
      </div>
      <div class="ws-source-status">
        <span class="ws-src-status-lbl" style="color:${statusColor}">${escapeHtml(statusText)}</span>
      </div>
    `;

    const enabledEl = card.querySelector('.ws-src-enabled');
    const labelEl = card.querySelector('.ws-src-label');
    const modeEl = card.querySelector('.ws-src-mode');
    const calEl = card.querySelector('.ws-src-cal');
    const pathEl = card.querySelector('.ws-src-path');
    const exclRow = card.querySelector('.ws-src-excl-row');
    const exclEl = card.querySelector('.ws-src-excl');
    const statusLbl = card.querySelector('.ws-src-status-lbl');
    const browseBtn = card.querySelector('.ws-src-browse');
    const delBtn = card.querySelector('.ws-src-del');

    const inspectNow = async () => {
      const p = pathEl.value.trim();
      src.path = p;
      src.mode = modeEl.value;
      src.exclude = exclEl.value.split(',').map(x => x.trim()).filter(Boolean);
      if (!p) {
        src.exists = false;
        statusLbl.style.color = 'var(--warning-text)';
        statusLbl.textContent = 'Choose or enter a folder path';
        return;
      }
      try {
        const info = await api('/api/config/inspect_folder', {
          method: 'POST',
          body: { path: p, mode: src.mode, exclude: src.exclude }
        });
        src.exists = !!info.exists;
        src.item_count = info.count || 0;
        src.subfolders = info.items || [];
        statusLbl.style.color = src.exists ? 'var(--success-text)' : 'var(--warning-text)';
        statusLbl.textContent = src.exists
          ? (src.mode === 'subfolders'
              ? `✓ Found (${src.item_count} subfolder categories${src.subfolders.length ? ': ' + src.subfolders.slice(0, 6).join(', ') : ''})`
              : `✓ Found (${src.item_count} top-level files/folders)`)
          : '⚠ Folder not found on disk';
      } catch (_) {}
    };

    enabledEl.onchange = () => { src.enabled = enabledEl.checked; };
    labelEl.oninput = () => { src.label = labelEl.value; };
    calEl.oninput = () => { src.calendar = calEl.value; };
    modeEl.onchange = () => {
      src.mode = modeEl.value;
      exclRow.style.display = src.mode === 'subfolders' ? 'flex' : 'none';
      inspectNow();
    };
    pathEl.onchange = inspectNow;
    exclEl.onchange = inspectNow;

    browseBtn.onclick = async () => {
      browseBtn.disabled = true;
      browseBtn.textContent = 'Opening…';
      try {
        const res = await api('/api/config/pick_folder', {
          method: 'POST',
          body: { prompt: `Select folder for "${src.label || 'Analysis'}":` }
        });
        if (res && res.ok && res.path) {
          pathEl.value = res.path;
          if (!labelEl.value.trim() || labelEl.value === 'New Folder') {
            labelEl.value = res.suggested_label || 'Folder';
            src.label = labelEl.value;
          }
          await inspectNow();
        }
      } catch (e) {
        showToast('Folder picker error: ' + e.message, 'error');
      } finally {
        browseBtn.disabled = false;
        browseBtn.textContent = '📁 Browse…';
      }
    };

    delBtn.onclick = () => {
      wsDraftSources.splice(idx, 1);
      renderWsSourcesList();
    };

    container.appendChild(card);
  });
}

let currentInspectedEvent = null;

function getInspectorSelection() {
  const dateEl = document.getElementById('m-ev-date');
  const startEl = document.getElementById('m-ev-start');
  const minEl = document.getElementById('m-ev-minutes');
  if (!dateEl || !startEl || !minEl || !dateEl.value || !startEl.value) return null;
  const mins = parseInt(minEl.value, 10);
  if (!mins || mins < 5) return null;
  const start = new Date(`${dateEl.value}T${startEl.value}:00`);
  if (isNaN(start)) return null;
  const end = new Date(start.getTime() + mins * 60000);
  return {
    start,
    end,
    mins,
    startIso: `${fmtDate(start)}T${fmtTime(start)}:00`,
    endIso: `${fmtDate(end)}T${fmtTime(end)}:00`
  };
}

function refreshEventInspectorTime() {
  const item = currentInspectedEvent;
  if (!item) return;
  const timeEl = document.getElementById('m-ev-time');
  const durEl = document.getElementById('m-ev-dur-label');
  const warnEl = document.getElementById('m-ev-warn');
  if (warnEl) warnEl.classList.remove('visible');

  if (item.all_day) {
    const dateStr = (item.start || '').slice(0, 10);
    timeEl.textContent = `${dateStr} (All day)`;
    durEl.textContent = 'All day';
    return;
  }

  const sel = getInspectorSelection();
  if (!sel) {
    timeEl.textContent = 'Pick a valid date, start time, and duration';
    durEl.textContent = '';
    return;
  }
  timeEl.textContent = `${fmtNice(sel.start)} · ${fmtTime(sel.start)} – ${fmtTime(sel.end)}`;
  const h = Math.floor(sel.mins / 60), m = sel.mins % 60;
  durEl.textContent = `${h ? h + ' h ' : ''}${m ? m + ' min' : ''}`.trim();

  if (warnEl) {
    const fakeBlock = { id: item.block_id || item.id };
    const hits = findConflicts(sel.start, sel.end, fakeBlock).filter(label => !label.startsWith((item.title || '') + ' ('));
    if (hits.length > 0) {
      warnEl.innerHTML = 'Overlaps with: ' + hits.map(escapeHtml).join(' · ');
      warnEl.classList.add('visible');
    }
  }
}

function updateInspectorQuickLinks() {
  const item = currentInspectedEvent;
  if (!item) return;

  const locVal = (document.getElementById('m-ev-location')?.value || '').trim();
  const urlVal = (document.getElementById('m-ev-url')?.value || '').trim();
  const notesVal = document.getElementById('m-ev-notes-input')?.value || '';

  const tempInfo = extractEventRichInfo({
    ...item,
    location: locVal,
    url: urlVal,
    notes: notesVal
  }, item.isProposedBlock);

  const quickBar = document.getElementById('m-ev-quick-bar');
  const btnVideo = document.getElementById('m-ev-btn-video');
  const btnMaps = document.getElementById('m-ev-btn-maps');
  const btnUrl = document.getElementById('m-ev-btn-url');
  const btnOpenTask = document.getElementById('m-ev-btn-open-task');
  const btnLogHabit = document.getElementById('m-ev-btn-log-habit');
  const btnEditHabit = document.getElementById('m-ev-btn-edit-habit');
  const linksContainer = document.getElementById('m-ev-detected-links');

  let anyQuickAction = false;

  if (btnVideo) {
    if (tempInfo.videoUrl) {
      btnVideo.href = tempInfo.videoUrl;
      btnVideo.textContent = `🎥 Join ${tempInfo.videoProvider || 'Video Call'}`;
      btnVideo.style.display = 'inline-flex';
      anyQuickAction = true;
    } else {
      btnVideo.style.display = 'none';
    }
  }

  if (btnMaps) {
    if (tempInfo.cleanLocation) {
      btnMaps.href = `https://maps.apple.com/?q=${encodeURIComponent(tempInfo.cleanLocation)}`;
      btnMaps.title = `Open "${tempInfo.cleanLocation}" in Maps`;
      btnMaps.style.display = 'inline-flex';
      anyQuickAction = true;
    } else {
      btnMaps.style.display = 'none';
    }
  }

  const nonVideoUrl = tempInfo.allUrls.find(u => u !== tempInfo.videoUrl);
  if (btnUrl) {
    if (nonVideoUrl) {
      btnUrl.href = nonVideoUrl;
      btnUrl.style.display = 'inline-flex';
      anyQuickAction = true;
    } else {
      btnUrl.style.display = 'none';
    }
  }

  if (btnOpenTask) {
    if (tempInfo.taskId) {
      btnOpenTask.style.display = 'inline-flex';
      anyQuickAction = true;
    } else {
      btnOpenTask.style.display = 'none';
    }
  }

  if (btnLogHabit) {
    if (tempInfo.linkedHabit) {
      btnLogHabit.style.display = 'inline-flex';
      anyQuickAction = true;
    } else {
      btnLogHabit.style.display = 'none';
    }
  }

  if (btnEditHabit) {
    if (tempInfo.linkedHabit) {
      btnEditHabit.style.display = 'inline-flex';
      anyQuickAction = true;
    } else {
      btnEditHabit.style.display = 'none';
    }
  }

  if (quickBar) {
    quickBar.style.display = anyQuickAction ? 'flex' : 'none';
  }

  if (linksContainer) {
    if (tempInfo.allUrls.length > 0) {
      linksContainer.style.display = 'flex';
      linksContainer.innerHTML = tempInfo.allUrls.map(u => {
        const prov = detectVideoProvider(u);
        let host = u;
        try { host = new URL(u).hostname.replace(/^www\./, ''); } catch (e) {}
        const label = prov ? `🎥 ${prov} (${host})` : `🔗 ${host}`;
        return `<a class="ev-detected-link-chip" href="${escapeHtml(u)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(u)}">${escapeHtml(label)} ↗</a>`;
      }).join('');
    } else {
      linksContainer.style.display = 'none';
      linksContainer.innerHTML = '';
    }
  }
}

function renderInspectorTaskCard(task) {
  const card = document.getElementById('m-ev-task-card');
  if (!card) return;
  if (!task) {
    card.style.display = 'none';
    return;
  }
  card.style.display = 'flex';
  const titleEl = document.getElementById('m-ev-task-title');
  const metaEl = document.getElementById('m-ev-task-meta');
  const summaryEl = document.getElementById('m-ev-task-summary');
  const subtasksEl = document.getElementById('m-ev-task-subtasks');
  const filesEl = document.getElementById('m-ev-task-files');

  if (titleEl) titleEl.textContent = task.title || task.input_text || `Task #${task.id}`;
  if (metaEl) {
    const parts = [];
    if (task.complexity) parts.push(task.complexity);
    if (task.total_minutes) parts.push(`${task.total_minutes} min est.`);
    if (task.deadline) parts.push(`Due ${task.deadline.replace('T', ' ').slice(0, 16)}`);
    metaEl.textContent = parts.join(' · ');
  }

  if (summaryEl) {
    if (task.summary && task.summary.trim()) {
      summaryEl.style.display = 'block';
      summaryEl.textContent = task.summary.trim();
    } else {
      summaryEl.style.display = 'none';
    }
  }

  if (subtasksEl) {
    const sts = task.subtasks || [];
    if (sts.length > 0) {
      subtasksEl.style.display = 'flex';
      const doneCount = sts.filter(s => s.status === 'done' || s.done).length;
      subtasksEl.innerHTML = `<div style="font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:2px">Subtasks (${doneCount}/${sts.length})</div>`;
      sts.forEach(st => {
        const isDone = st.status === 'done' || !!st.done;
        const row = document.createElement('label');
        row.className = `ev-subtask-row ${isDone ? 'done' : ''}`;
        const tookStr = isDone && st.actual_minutes ? `, took ${st.actual_minutes}m` : '';
        row.innerHTML = `
          <input type="checkbox" ${isDone ? 'checked' : ''}>
          <span>${escapeHtml(st.title)}${st.minutes ? ` <small style="color:var(--text-dim)">(${st.minutes}m${tookStr})</small>` : ''}</span>
        `;
        const cb = row.querySelector('input');
        cb.addEventListener('change', async () => {
          if (cb.checked) {
            openCheckinDialog({
              title: 'Complete Subtask',
              itemName: st.title,
              plannedMinutes: st.actual_minutes || st.minutes || 30,
              showDate: false,
              showNote: false,
              onSave: async ({ minutes }) => {
                await api(`/api/tasks/${task.id}/subtasks/${st.id}/done`, {
                  method: 'POST',
                  body: { actual_minutes: minutes, done: true }
                });
                st.status = 'done';
                st.actual_minutes = minutes;
                row.classList.add('done');
                loadState();
              },
              onCancel: () => {
                cb.checked = false;
              }
            });
          } else {
            try {
              await api(`/api/tasks/${task.id}/subtasks/${st.id}/done`, {
                method: 'POST',
                body: { done: false }
              });
              st.status = 'todo';
              st.actual_minutes = null;
              row.classList.remove('done');
              loadState();
            } catch (err) {
              showToast('Failed to update subtask: ' + err.message, 'error');
            }
          }
        });
        subtasksEl.appendChild(row);
      });
    } else {
      subtasksEl.style.display = 'none';
      subtasksEl.innerHTML = '';
    }
  }

  if (filesEl) {
    const files = task.files_used || [];
    if (files.length > 0) {
      filesEl.style.display = 'flex';
      filesEl.innerHTML = files.map(f => {
        const shortName = String(f).split('/').slice(-2).join('/');
        return `<span class="ev-file-chip" title="${escapeHtml(String(f))}">📄 ${escapeHtml(shortName)}</span>`;
      }).join('');
    } else {
      filesEl.style.display = 'none';
      filesEl.innerHTML = '';
    }
  }
}

function initEventInspector() {
  const modal = document.getElementById('modal-event');
  const btnClose = document.getElementById('modal-event-close');
  const btnCancel = document.getElementById('modal-event-cancel');
  const btnSave = document.getElementById('m-ev-btn-save');
  const btnDelete = document.getElementById('m-ev-btn-delete');
  const btnDeleteSeries = document.getElementById('m-ev-btn-delete-series');
  const btnOpenTask = document.getElementById('m-ev-btn-open-task');
  const btnLogHabit = document.getElementById('m-ev-btn-log-habit');
  const btnEditHabit = document.getElementById('m-ev-btn-edit-habit');
  const btnResched = document.getElementById('m-ev-btn-resched');
  const btnAiResched = document.getElementById('m-ev-btn-ai-resched');
  const inputAiResched = document.getElementById('m-ev-ai-input');
  const btnSwap = document.getElementById('m-ev-btn-swap');

  if (!modal) return;

  function closeModal() {
    modal.style.display = 'none';
    currentInspectedEvent = null;
  }

  btnClose.addEventListener('click', closeModal);
  btnCancel.addEventListener('click', closeModal);

  const dateEl = document.getElementById('m-ev-date');
  const startEl = document.getElementById('m-ev-start');
  const minEl = document.getElementById('m-ev-minutes');
  [dateEl, startEl, minEl].forEach(el => {
    if (el) {
      el.addEventListener('input', refreshEventInspectorTime);
      el.addEventListener('change', refreshEventInspectorTime);
    }
  });

  ['m-ev-location', 'm-ev-url', 'm-ev-notes-input'].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener('input', updateInspectorQuickLinks);
      el.addEventListener('change', updateInspectorQuickLinks);
    }
  });

  document.querySelectorAll('#m-ev-quick-shift [data-ev-shift]').forEach(btn => {
    btn.addEventListener('click', () => {
      const sel = getInspectorSelection();
      if (!sel) return;
      const next = new Date(sel.start.getTime() + parseInt(btn.dataset.evShift, 10) * 60000);
      dateEl.value = fmtDate(next);
      startEl.value = fmtTime(next);
      refreshEventInspectorTime();
    });
  });

  if (btnResched) {
    btnResched.addEventListener('click', () => {
      const sel = getInspectorSelection();
      if (!sel || !currentInspectedEvent) return;
      const DAY_START = 8, DAY_END = 20;
      let cand = new Date(sel.start.getTime() + 15 * 60000);
      const fakeBlock = { id: currentInspectedEvent.block_id || currentInspectedEvent.id };
      for (let guard = 0; guard < 14 * 24 * 4; guard++) {
        const startMin = cand.getHours() * 60 + cand.getMinutes();
        const end = new Date(cand.getTime() + sel.mins * 60000);
        const endMin = startMin + sel.mins;
        if (startMin < DAY_START * 60) { cand.setHours(DAY_START, 0, 0, 0); continue; }
        if (endMin > DAY_END * 60) { cand.setDate(cand.getDate() + 1); cand.setHours(DAY_START, 0, 0, 0); continue; }
        if (cand > new Date() && findConflicts(cand, end, fakeBlock).length === 0) {
          dateEl.value = fmtDate(cand);
          startEl.value = fmtTime(cand);
          refreshEventInspectorTime();
          return;
        }
        cand = new Date(cand.getTime() + 15 * 60000);
      }
      showToast('No free slot found in the next 2 weeks', 'warning');
    });
  }

  if (btnAiResched && inputAiResched) {
    const runCalendarAiResched = async () => {
      if (!currentInspectedEvent || !currentInspectedEvent.task_id) return;
      const text = inputAiResched.value.trim();
      if (!text) return showToast('Type how you want to reschedule this task', 'warning');
      const tid = currentInspectedEvent.task_id;
      const cat = currentInspectedEvent.category || (currentInspectedEvent._linkedTask ? currentInspectedEvent._linkedTask.category : '');
      btnAiResched.disabled = true;
      btnAiResched.textContent = 'Adjusting...';
      closeModal();
      showToast('Agent is rescheduling task subtasks...', 'info');
      try {
        await startTaskPlanning(text, cat, tid);
      } catch (err) {
        showToast('Reschedule failed: ' + err.message, 'error');
      } finally {
        btnAiResched.disabled = false;
        btnAiResched.textContent = 'Reschedule with AI';
      }
    };
    btnAiResched.addEventListener('click', runCalendarAiResched);
    inputAiResched.addEventListener('keydown', e => {
      if (e.key === 'Enter') runCalendarAiResched();
    });
  }

  if (btnSwap) {
    btnSwap.addEventListener('click', async () => {
      if (!currentInspectedEvent || !currentInspectedEvent.block_id) return;
      const selectEl = document.getElementById('m-ev-swap-select');
      const otherBid = selectEl ? parseInt(selectEl.value, 10) : 0;
      if (!otherBid) return showToast('Select a block to swap with', 'warning');
      btnSwap.disabled = true;
      try {
        const res = await api('/api/calendar/blocks/swap', {
          method: 'POST',
          body: {
            block_a_id: currentInspectedEvent.block_id,
            block_b_id: otherBid
          }
        });
        showToast(res.summary || 'Swapped calendar slots', 'success');
        closeModal();
        loadState();
      } catch (e) {
        showToast('Failed to swap slots: ' + e.message, 'error');
      } finally {
        btnSwap.disabled = false;
      }
    });
  }

  btnSave.addEventListener('click', async () => {
    if (!currentInspectedEvent) return;
    const item = currentInspectedEvent;
    const newTitle = document.getElementById('m-ev-title').value.trim();
    if (!newTitle) return showToast('Title cannot be empty', 'warning');

    const newLoc = (document.getElementById('m-ev-location')?.value || '').trim();
    const newUrl = (document.getElementById('m-ev-url')?.value || '').trim();
    const userNotes = (document.getElementById('m-ev-notes-input')?.value || '').trim();
    const internalTags = item._internalTags || [];
    const combinedNotes = [userNotes, ...internalTags].filter(Boolean).join('\n');

    const sel = !item.all_day ? getInspectorSelection() : null;

    btnSave.disabled = true;
    try {
      let realignedCount = 0;
      if (item.isProposedBlock) {
        if (newTitle !== (item.title || '')) {
          await api(`/api/tasks/${item.task_id}/rename`, {
            method: 'POST',
            body: { title: newTitle }
          });
        }
        if (sel) {
          const mvRes = await api(`/api/tasks/${item.task_id}/move_block`, {
            method: 'POST',
            body: { block_id: item.block_id || item.id, new_start: sel.startIso, new_end: sel.endIso }
          });
          if (mvRes && mvRes.block && Array.isArray(mvRes.block.realigned)) {
            realignedCount = mvRes.block.realigned.length;
          }
        }
      } else {
        const upRes = await api('/api/calendar/events/update', {
          method: 'POST',
          body: {
            event_id: item.id,
            item_id: item.item_id,
            ext_id: item.ext_id,
            habit_id: item.habit_id,
            block_id: item.block_id,
            title: newTitle,
            new_start: sel ? sel.startIso : undefined,
            new_end: sel ? sel.endIso : undefined,
            location: newLoc,
            url: newUrl,
            notes: combinedNotes,
            occurrence: item.occurrence,
            span: 'this'
          }
        });
        if (upRes && Array.isArray(upRes.realigned)) {
          realignedCount = upRes.realigned.length;
        }
      }
      if (realignedCount > 0) {
        showToast(`Event updated · Realigned ${realignedCount} subtask(s) to preserve sequential order`, 'success');
      } else {
        showToast('Event updated', 'success');
      }
      closeModal();
      loadState();
    } catch (e) {
      showToast('Failed to update event: ' + e.message, 'error');
    } finally {
      btnSave.disabled = false;
    }
  });

  btnDelete.addEventListener('click', async () => {
    if (!currentInspectedEvent) return;
    const item = currentInspectedEvent;
    const msg = item.recurring
      ? `Delete only this occurrence of "${item.title}" (${(item.start || '').slice(0, 10)})?`
      : `Delete "${item.title}" from calendar?`;
    const ok = await uiConfirm(msg, 'Delete Event');
    if (!ok) return;

    btnDelete.disabled = true;
    try {
      if (item.isProposedBlock) {
        await api(`/api/tasks/${item.task_id}/blocks/${item.id}`, { method: 'DELETE' });
      } else {
        await api('/api/calendar/events/delete', {
          method: 'POST',
          body: {
            event_id: item.id,
            item_id: item.item_id,
            ext_id: item.ext_id,
            occurrence: item.occurrence,
            start: item.start,
            span: 'this',
            block_id: item.block_id,
            habit_id: item.habit_id
          }
        });
      }
      showToast('Event deleted', 'info');
      closeModal();
      loadState();
    } catch (e) {
      showToast('Failed to delete: ' + e.message, 'error');
    } finally {
      btnDelete.disabled = false;
    }
  });

  btnDeleteSeries.addEventListener('click', async () => {
    if (!currentInspectedEvent) return;
    const item = currentInspectedEvent;
    const ok = await uiConfirm(`Permanently delete the entire recurring series for "${item.title}"?`, 'Delete Entire Series');
    if (!ok) return;

    btnDeleteSeries.disabled = true;
    try {
      await api('/api/calendar/events/delete', {
        method: 'POST',
        body: {
          event_id: item.id,
          item_id: item.item_id,
          ext_id: item.ext_id,
          occurrence: item.occurrence,
          start: item.start,
          span: 'all',
          habit_id: item.habit_id,
          block_id: item.block_id
        }
      });
      showToast('Recurring series deleted', 'info');
      closeModal();
      loadState();
    } catch (e) {
      showToast('Failed to delete series: ' + e.message, 'error');
    } finally {
      btnDeleteSeries.disabled = false;
    }
  });

  if (btnOpenTask) {
    btnOpenTask.addEventListener('click', () => {
      if (currentInspectedEvent && currentInspectedEvent.task_id) {
        const tid = currentInspectedEvent.task_id;
        closeModal();
        const tabBtn = document.querySelector('.nav-tab[data-tab="tasks"]');
        if (tabBtn) tabBtn.click();
        openTaskDrawer(tid);
      }
    });
  }

  if (btnLogHabit) {
    btnLogHabit.addEventListener('click', () => {
      const h = currentInspectedEvent && currentInspectedEvent._linkedHabit;
      if (!h) return;
      const evDate = (currentInspectedEvent.start || '').slice(0, 10) || fmtDate(new Date());
      closeModal();
      openCheckinDialog({
        title: 'Habit Check-in',
        itemName: h.name,
        plannedMinutes: h.minutes || 45,
        defaultDate: evDate,
        showDate: true,
        showNote: true,
        onSave: async ({ minutes, date, note }) => {
          try {
            await api(`/api/habits/${h.id}/checkin`, {
              method: 'POST',
              body: { status: 'done', actual_minutes: minutes, date, note }
            });
            showToast(`Habit '${h.name}' check-in saved`, 'success');
            loadState();
          } catch (e) {
            showToast('Check-in failed: ' + e.message, 'error');
          }
        }
      });
    });
  }

  if (btnEditHabit) {
    btnEditHabit.addEventListener('click', () => {
      const h = currentInspectedEvent && currentInspectedEvent._linkedHabit;
      if (!h) return;
      closeModal();
      openHabitModal(h);
    });
  }
}

function openEventInspector(item, isBlock = false) {
  const info = extractEventRichInfo(item, isBlock);
  const enriched = {
    ...item,
    isProposedBlock: isBlock,
    block_id: info.blockId,
    task_id: info.taskId,
    habit_id: info.habitId,
    _linkedTask: info.linkedTask,
    _linkedHabit: info.linkedHabit,
    _internalTags: info.internalTags
  };

  currentInspectedEvent = enriched;
  const modal = document.getElementById('modal-event');
  if (!modal) return;

  const isWritable = enriched.writable !== false;

  // Header badge & title
  const typeBadge = document.getElementById('m-ev-type-badge');
  const headerTitle = document.getElementById('m-ev-calendar-title');
  if (typeBadge) {
    if (enriched.task_id || isBlock) {
      const isUni = getBlockCategory(enriched, isBlock) === 'university';
      const label = isBlock && item.isProposed ? 'Draft Block' : 'Task Block';
      typeBadge.textContent = `${label} · ${isUni ? 'University' : 'Thesis'}`;
      typeBadge.className = `ev-type-pill block ${isUni ? 'block-uni' : 'block-thesis'}`;
    } else if (enriched.habit_id || (enriched.calendar || '').toLowerCase().includes('habits')) {
      typeBadge.textContent = 'Habit';
      typeBadge.className = 'ev-type-pill habit';
    } else if (enriched.all_day) {
      typeBadge.textContent = 'All-Day';
      typeBadge.className = 'ev-type-pill meeting';
    } else {
      typeBadge.textContent = 'Event';
      typeBadge.className = 'ev-type-pill meeting';
    }
  }
  if (headerTitle) {
    headerTitle.textContent = enriched.title || 'Calendar Event Details';
  }

  const titleInput = document.getElementById('m-ev-title');
  titleInput.value = enriched.title || '';
  titleInput.disabled = !isWritable;

  const srcBadge = enriched.source ? ` (${enriched.source})` : '';
  const roBadge = !isWritable ? ' [Read-only]' : '';
  document.getElementById('m-ev-cal-name').textContent = (enriched.calendar || (isBlock ? 'Planned Block' : 'Calendar')) + srcBadge + roBadge;

  // Recurrence & Attendees metadata
  const recInfoEl = document.getElementById('m-ev-recurrence-info');
  if (recInfoEl) {
    if (enriched.recurring) {
      const rule = enriched.rule || {};
      const dayNames = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
      const daysStr = (rule.days || []).map(d => dayNames[d] || '').filter(Boolean).join(', ');
      recInfoEl.textContent = `↻ Recurring${rule.freq ? ` (${rule.freq}${daysStr ? ': ' + daysStr : ''})` : ''}`;
    } else {
      recInfoEl.textContent = '';
    }
  }

  const attEl = document.getElementById('m-ev-attendees-info');
  if (attEl) {
    const parts = [];
    if (enriched.organizer) parts.push(`<strong>Organizer:</strong> ${escapeHtml(enriched.organizer)}`);
    if (Array.isArray(enriched.attendees) && enriched.attendees.length > 0) {
      parts.push(`<strong>Participants:</strong> ${enriched.attendees.slice(0, 8).map(escapeHtml).join(', ')}${enriched.attendees.length > 8 ? ` +${enriched.attendees.length - 8} more` : ''}`);
    }
    if (parts.length > 0) {
      attEl.style.display = 'block';
      attEl.innerHTML = parts.join(' · ');
    } else {
      attEl.style.display = 'none';
      attEl.innerHTML = '';
    }
  }

  const timeControls = document.getElementById('m-ev-time-controls');
  const dateEl = document.getElementById('m-ev-date');
  const startEl = document.getElementById('m-ev-start');
  const minEl = document.getElementById('m-ev-minutes');

  if (enriched.all_day || !enriched.start || !enriched.end) {
    if (timeControls) timeControls.style.display = 'none';
  } else {
    if (timeControls) timeControls.style.display = 'block';
    const origStart = new Date(enriched.start);
    const origEnd = new Date(enriched.end);
    const origMin = Math.max(5, Math.round((origEnd - origStart) / 60000));
    dateEl.value = fmtDate(origStart);
    startEl.value = fmtTime(origStart);
    minEl.value = origMin;
    dateEl.disabled = !isWritable;
    startEl.disabled = !isWritable;
    minEl.disabled = !isWritable;
    document.querySelectorAll('#m-ev-quick-shift button').forEach(b => { b.disabled = !isWritable; });
  }
  refreshEventInspectorTime();

  // Populate Location, URL, and Notes fields
  const locInput = document.getElementById('m-ev-location');
  const urlInput = document.getElementById('m-ev-url');
  const notesInput = document.getElementById('m-ev-notes-input');
  const detailsGrid = document.getElementById('m-ev-details-grid');
  const notesGroup = document.getElementById('m-ev-notes-group');

  if (locInput) {
    locInput.value = info.cleanLocation || '';
    locInput.disabled = !isWritable || isBlock;
  }
  if (urlInput) {
    urlInput.value = info.rawUrl || info.videoUrl || '';
    urlInput.disabled = !isWritable || isBlock;
  }
  if (detailsGrid) {
    detailsGrid.style.display = isBlock ? 'none' : 'grid';
  }
  if (notesInput) {
    notesInput.value = info.cleanNotes || '';
    notesInput.disabled = !isWritable || isBlock;
  }
  if (notesGroup) {
    notesGroup.style.display = (isBlock && !info.cleanNotes) ? 'none' : 'block';
  }

  // Update quick links (Video Call, Maps, URL, Task/Habit buttons, Detected Links)
  updateInspectorQuickLinks();

  // Populate Linked Task Card (Summary, Subtasks, Grounded Files)
  renderInspectorTaskCard(info.linkedTask);

  const btnDelete = document.getElementById('m-ev-btn-delete');
  const seriesBtn = document.getElementById('m-ev-btn-delete-series');
  const btnSave = document.getElementById('m-ev-btn-save');
  const btnResched = document.getElementById('m-ev-btn-resched');
  const swapGroup = document.getElementById('m-ev-swap-group');
  const swapSelect = document.getElementById('m-ev-swap-select');

  btnDelete.disabled = !isWritable;
  btnDelete.textContent = enriched.recurring ? '✕ Delete Occurrence' : '✕ Delete Event';
  btnSave.disabled = !isWritable;

  if (enriched.recurring && isWritable) {
    seriesBtn.style.display = 'inline-flex';
  } else {
    seriesBtn.style.display = 'none';
  }

  if (btnResched) {
    btnResched.style.display = (isWritable && !enriched.all_day) ? 'inline-flex' : 'none';
  }

  const aiSec = document.getElementById('m-ev-ai-resched-sec');
  const aiBadge = document.getElementById('m-ev-ai-task-badge');
  const aiInput = document.getElementById('m-ev-ai-input');
  if (aiSec) {
    if (enriched.task_id) {
      aiSec.style.display = 'block';
      if (aiBadge) {
        const taskTitle = (info.linkedTask && info.linkedTask.title) ? info.linkedTask.title : `Task #${enriched.task_id}`;
        aiBadge.textContent = `Task #${enriched.task_id} (${taskTitle})`;
      }
      if (aiInput) aiInput.value = '';
    } else {
      aiSec.style.display = 'none';
    }
  }

  // Populate Swap Slot selector if this is a Task Block
  if (swapGroup && swapSelect) {
    if (isWritable && enriched.block_id) {
      const otherTaskOpts = [];
      const sameTaskOpts = [];
      (S.tasks || []).forEach(t => {
        if (t.status === 'done') return;
        (t.blocks || []).forEach(b => {
          if (b.id === enriched.block_id || !b.start || !b.end) return;
          const st = new Date(b.start);
          const label = `${fmtNice(st)} ${b.start.slice(11, 16)}–${b.end.slice(11, 16)} · ${b.title || t.title}`;
          const optHtml = `<option value="${b.id}">${escapeHtml(label)}</option>`;
          if (t.id === enriched.task_id) {
            sameTaskOpts.push({ start: b.start, html: optHtml });
          } else {
            otherTaskOpts.push({ start: b.start, html: optHtml });
          }
        });
      });
      otherTaskOpts.sort((a, b) => a.start.localeCompare(b.start));
      sameTaskOpts.sort((a, b) => a.start.localeCompare(b.start));

      const groups = [];
      if (otherTaskOpts.length > 0) {
        groups.push(`<optgroup label="Other Tasks' Blocks">${otherTaskOpts.map(o => o.html).join('')}</optgroup>`);
      }
      if (sameTaskOpts.length > 0) {
        groups.push(`<optgroup label="Other Blocks of This Task">${sameTaskOpts.map(o => o.html).join('')}</optgroup>`);
      }
      if (groups.length > 0) {
        swapSelect.innerHTML = groups.join('');
        swapGroup.style.display = 'block';
      } else {
        swapGroup.style.display = 'none';
      }
    } else {
      swapGroup.style.display = 'none';
    }
  }

  modal.style.display = 'flex';
}

String.prototype.title = function() {
  return this.charAt(0).toUpperCase() + this.slice(1).toLowerCase();
};
