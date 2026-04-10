'use strict';

const WS_URL = 'ws://127.0.0.1:8765';
const RECONNECT_DELAY = 2000;

const calOverlay   = document.getElementById('calibration-overlay');
const calFill      = document.getElementById('cal-progress-fill');
const mainLayout   = document.getElementById('main-layout');
const canvas       = document.getElementById('video-canvas');
const ctx          = canvas.getContext('2d');
const exerciseEl   = document.getElementById('exercise-label');
const severityEl   = document.getElementById('severity-badge');
const tipEl        = document.getElementById('tip-text');
const latencyEl    = document.getElementById('latency-label');
const startBtn     = document.getElementById('start-btn');
const stopBtn      = document.getElementById('stop-btn');
const reportBtn    = document.getElementById('report-btn');
const reportModal  = document.getElementById('report-modal');
const reportClose  = document.getElementById('report-close');
const reportLoading = document.getElementById('report-loading');
const reportContent = document.getElementById('report-content');
const reportMeta   = document.getElementById('report-meta');
const reportCats   = document.getElementById('report-categories');
const reportExs    = document.getElementById('report-exercises');
const reportInsights = document.getElementById('report-insights');
const reportPlan   = document.getElementById('report-plan');
const journalBody  = document.getElementById('journal-body');
const summaryDiv   = document.getElementById('session-summary');
const summaryExEl  = document.getElementById('summary-exercises');
const summaryRepsEl = document.getElementById('summary-reps');
const hrDisplay    = document.getElementById('hr-display');
const hrValueEl    = document.getElementById('hr-value');
const timerDiv     = document.getElementById('session-timer');
const timerValueEl = document.getElementById('timer-value');

let ws = null;
let calProgress = 0;
let calTimer = null;

// ── Session timer ─────────────────────────────────────────────────────────────
const timer = {
  _start:    null,
  _interval: null,

  _fmt(secs) {
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = secs % 60;
    if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    return `${m}:${String(s).padStart(2,'0')}`;
  },

  start() {
    this._start = Date.now();
    timerDiv.classList.remove('hidden', 'finished');
    timerValueEl.textContent = '0:00';
    this._interval = setInterval(() => {
      const elapsed = Math.floor((Date.now() - this._start) / 1000);
      timerValueEl.textContent = this._fmt(elapsed);
    }, 1000);
    log.info('Session timer started');
  },

  stop() {
    if (this._interval) {
      clearInterval(this._interval);
      this._interval = null;
    }
    timerDiv.classList.add('finished');
    const elapsed = this._start ? Math.floor((Date.now() - this._start) / 1000) : 0;
    timerValueEl.textContent = this._fmt(elapsed);
    log.info(`Session timer stopped — elapsed ${this._fmt(elapsed)}`);
  },

  reset() {
    this.stop();
    timerDiv.classList.add('hidden');
    timerValueEl.textContent = '0:00';
  },
};

// ── Frontend logger ───────────────────────────────────────────────────────────
const log = {
  _ts: () => new Date().toISOString().slice(11, 23), // HH:MM:SS.mmm
  info:  (msg, ...args) => console.log( `[${log._ts()}] [INFO ]`, msg, ...args),
  warn:  (msg, ...args) => console.warn(`[${log._ts()}] [WARN ]`, msg, ...args),
  error: (msg, ...args) => console.error(`[${log._ts()}] [ERROR]`, msg, ...args),
  debug: (msg, ...args) => console.debug(`[${log._ts()}] [DEBUG]`, msg, ...args),
};

// ── FPS tracking ──────────────────────────────────────────────────────────────
const fps = {
  count: 0,
  windowStart: performance.now(),
  total: 0,
  lastFps: 0,

  tick() {
    this.count++;
    this.total++;
    const now = performance.now();
    const elapsed = (now - this.windowStart) / 1000;
    if (elapsed >= 5.0) {
      this.lastFps = this.count / elapsed;
      log.debug(`Render FPS: ${this.lastFps.toFixed(1)}  (total_frames=${this.total})`);
      this.count = 0;
      this.windowStart = now;
    }
  },
};

// ── Analysis stats ────────────────────────────────────────────────────────────
const stats = {
  analysisCount: 0,
  warningCount:  0,
  criticalCount: 0,
  lastExercise:  null,
  connectTime:   null,
};

// ── Calibration progress animation ───────────────────────────────────────────
function startCalAnimation() {
  calProgress = 0;
  calFill.style.width = '0%';
  calTimer = setInterval(() => {
    calProgress = Math.min(calProgress + 2, 95); // don't reach 100 until done
    calFill.style.width = calProgress + '%';
  }, 100);
  log.info('Calibration animation started');
}

function stopCalAnimation(success) {
  clearInterval(calTimer);
  calFill.style.width = success ? '100%' : '0%';
  log.info(`Calibration animation stopped — success=${success}`);
}

// ── WebSocket ────────────────────────────────────────────────────────────────
function connect() {
  log.info(`Connecting to ${WS_URL}`);
  ws = new WebSocket(WS_URL);

  ws.addEventListener('open', () => {
    stats.connectTime = performance.now();
    log.info('WebSocket connected');
  });

  ws.addEventListener('message', (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch (e) {
      log.warn('Received non-JSON message — ignored', e);
      return;
    }

    switch (msg.type) {
      case 'calibrating':
        log.info('Calibration started — waiting for user to enter frame');
        calOverlay.classList.remove('hidden');
        startCalAnimation();
        break;

      case 'calibration_done':
        log.info('Calibration complete — proportions acquired');
        stopCalAnimation(true);
        setTimeout(() => calOverlay.classList.add('hidden'), 400);
        break;

      case 'calibration_failed':
        log.warn(`Calibration failed: ${msg.reason}`);
        stopCalAnimation(false);
        setTimeout(() => calOverlay.classList.add('hidden'), 400);
        tipEl.textContent = '\u26a0\ufe0f Calibration failed \u2014 angle indicators disabled';
        break;

      case 'frame':
        renderFrame(msg.data);
        break;

      case 'analysis':
        logAnalysis(msg);
        updateSidebar(msg);
        addJournalRow(msg);
        break;

      case 'session_started':
        timer.start();
        break;

      case 'session_summary': {
        timer.stop();
        summaryDiv.style.display = 'block';
        summaryExEl.textContent =
          'Exercises: ' + (msg.exercises.length ? msg.exercises.join(', ') : 'Unknown');
        summaryRepsEl.textContent = 'Total Reps: ' + msg.total_reps;
        // Add summary row to journal
        const tr = document.createElement('tr');
        const ts = new Date().toLocaleTimeString();
        tr.innerHTML = `<td>${ts}</td><td>${msg.exercises.join(', ') || 'Unknown'}</td><td>Summary</td>`;
        journalBody.prepend(tr);
        log.info(
          'Session summary received — exercises=' + (msg.exercises.join(',') || 'none') +
          ' total_reps=' + msg.total_reps
        );
        break;
      }

      case 'heart_rate':
        hrDisplay.style.display = 'inline';
        hrValueEl.textContent = msg.bpm;
        log.debug(`Heart rate: ${msg.bpm} bpm`);
        break;

      case 'sessions_list':
        renderSessionList(msg.sessions);
        break;

      case 'reanalyze_progress':
        reanalyzeBar.style.width = msg.pct + '%';
        reanalyzeLabel.textContent = msg.msg;
        break;

      case 'reanalyze_done':
        renderReanalyzeResult(msg.result);
        break;

      case 'reanalyze_error':
        reanalyzeProgressWrap.classList.add('hidden');
        reanalyzeResult.classList.remove('hidden');
        reanalyzeResult.innerHTML = `<p style="color:#c62828">⚠️ ${msg.reason}</p>`;
        document.querySelectorAll('.session-card button').forEach(b => {
          b.disabled = false; b.textContent = 'Analyze';
        });
        break;

      case 'report_loading':
        log.info('Report generation started on backend');
        break;

      case 'report':
        log.info('Report received');
        renderReport(msg.data);
        break;

      case 'report_error':
        log.error(`Report generation failed: ${msg.reason}`);
        reportLoading.classList.add('hidden');
        reportContent.classList.remove('hidden');
        reportMeta.innerHTML = `<p style="color:#c62828">⚠️ ${msg.reason}</p>`;
        break;

      case 'error':
        log.error(`Server error: ${msg.reason}`);
        break;

      default:
        log.debug(`Unhandled message type: ${msg.type}`);
    }
  });

  ws.addEventListener('close', (evt) => {
    const uptime = stats.connectTime
      ? ((performance.now() - stats.connectTime) / 1000).toFixed(1)
      : '—';
    log.warn(
      `WebSocket disconnected — code=${evt.code}  reason=${evt.reason || '(none)'}` +
      `  uptime=${uptime}s — retrying in ${RECONNECT_DELAY}ms`
    );
    setTimeout(connect, RECONNECT_DELAY);
  });

  ws.addEventListener('error', (e) => {
    log.error('WebSocket error', e);
  });
}

// ── Analysis event logger ─────────────────────────────────────────────────────
function logAnalysis({ severity, issues, tip, latency_s }) {
  stats.analysisCount++;
  if (severity === 'WARNING')  stats.warningCount++;
  if (severity === 'CRITICAL') stats.criticalCount++;

  const issueStr = (issues && issues.length) ? issues.join(' | ') : 'none';
  const logFn = severity === 'CRITICAL' ? log.error
              : severity === 'WARNING'  ? log.warn
              : log.info;

  logFn(
    `Analysis #${stats.analysisCount} — latency=${latency_s}s` +
    `  severity=${severity}  issues=${issueStr}` +
    `  (warnings=${stats.warningCount}  criticals=${stats.criticalCount})`
  );
  if (tip) {
    log.info(`  → tip: ${tip}`);
  }
}

// ── Frame rendering ───────────────────────────────────────────────────────────
function renderFrame(b64) {
  const img = new Image();
  img.onload = () => {
    const w = canvas.clientWidth  || img.naturalWidth  || 640;
    const h = canvas.clientHeight || img.naturalHeight || 480;
    if (canvas.width !== w || canvas.height !== h) {
      log.debug(`Canvas resized: ${canvas.width}×${canvas.height} → ${w}×${h}`);
      canvas.width  = w;
      canvas.height = h;
    }
    ctx.drawImage(img, 0, 0, w, h);
    fps.tick();
  };
  img.onerror = (e) => {
    log.warn('Failed to decode frame image', e);
  };
  img.src = 'data:image/jpeg;base64,' + b64;
}

// ── Sidebar updates ───────────────────────────────────────────────────────────
function updateSidebar({ severity, tip, latency_s }) {
  tipEl.textContent      = tip || '';
  latencyEl.textContent  = latency_s != null ? `Last analysis: ${latency_s}s` : '';

  severityEl.textContent = severity || 'OK';
  severityEl.className   = 'badge ' + (severity || 'ok').toLowerCase();
}

function addJournalRow({ severity, issues }) {
  const tr = document.createElement('tr');
  const ts = new Date().toLocaleTimeString();
  const issueText = (issues && issues.length) ? issues[0] : '—';
  tr.innerHTML = `<td>${ts}</td><td>${issueText}</td><td>${severity}</td>`;
  journalBody.prepend(tr);
  // Keep journal to last 50 rows
  while (journalBody.children.length > 50) journalBody.removeChild(journalBody.lastChild);
}

// ── Session controls ──────────────────────────────────────────────────────────
startBtn.addEventListener('click', () => {
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'start_session' }));
    log.info('start_session sent to server');
    startBtn.disabled = true;
    stopBtn.disabled  = false;
    summaryDiv.style.display = 'none';
    hrDisplay.style.display  = 'none';
    hrValueEl.textContent    = '--';
    timer.reset();
  } else {
    log.warn('start_session clicked but WebSocket is not open (state=' + ws?.readyState + ')');
  }
});

stopBtn.addEventListener('click', () => {
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'stop_session' }));
    log.info('stop_session sent to server');
    startBtn.disabled = false;
    stopBtn.disabled  = true;
  } else {
    log.warn('stop_session clicked but WebSocket is not open (state=' + ws?.readyState + ')');
  }
});

// ── Weekly Report ─────────────────────────────────────────────────────────────

const CAT_CLASS = {
  'Upper Body': 'cat-upper',
  'Lower Body': 'cat-lower',
  'Core':       'cat-core',
  'Cardio':     'cat-cardio',
  'General':    'cat-general',
};

function openReportModal() {
  reportModal.classList.remove('hidden');
  reportLoading.classList.remove('hidden');
  reportContent.classList.add('hidden');
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'generate_report', days: 30 }));
    log.info('generate_report sent');
  } else {
    log.warn('Cannot generate report — WebSocket not open');
  }
}

function closeReportModal() {
  reportModal.classList.add('hidden');
}

function renderReport(data) {
  // ── Meta row ──
  const hrs = Math.floor(data.total_minutes / 60);
  const mins = data.total_minutes % 60;
  const timeStr = hrs > 0 ? `${hrs}h ${mins}m` : `${data.total_minutes}m`;
  reportMeta.innerHTML = `
    <div class="meta-card">
      <div class="val">${data.total_sessions}</div>
      <div class="lbl">Sessions</div>
    </div>
    <div class="meta-card">
      <div class="val">${timeStr}</div>
      <div class="lbl">Total Time</div>
    </div>
    <div class="meta-card">
      <div class="val">${data.exercises.length}</div>
      <div class="lbl">Exercises</div>
    </div>
    <div class="meta-card">
      <div class="val">${data.period_days}d</div>
      <div class="lbl">Period</div>
    </div>`;

  // ── Category bars ──
  const maxCat = Math.max(1, ...Object.values(data.category_counts));
  const catOrder = ['Upper Body', 'Lower Body', 'Core', 'Cardio', 'General'];
  reportCats.innerHTML = '<h3>Body-Part Coverage</h3>' +
    catOrder.map(cat => {
      const count = data.category_counts[cat] ?? 0;
      const pct   = Math.round((count / maxCat) * 100);
      const cls   = CAT_CLASS[cat] || 'cat-general';
      return `<div class="cat-row">
        <span class="cat-label">${cat}</span>
        <div class="cat-bar-wrap">
          <div class="cat-bar ${cls}" style="width:${pct}%"></div>
        </div>
        <span class="cat-count">${count}</span>
      </div>`;
    }).join('');

  // ── Exercise chips ──
  if (data.exercises.length) {
    reportExs.innerHTML = '<h3>Exercises Performed</h3><div class="ex-chips">' +
      data.exercises.map(e => {
        const cls = CAT_CLASS[e.category] || 'cat-general';
        return `<span class="ex-chip">
          <span class="ex-dot ${cls}"></span>
          ${e.name} <span style="color:#666">&times;${e.sessions}</span>
        </span>`;
      }).join('') + '</div>';
  } else {
    reportExs.innerHTML = '<h3>Exercises Performed</h3><p style="color:#666">No exercise data yet — complete a session first.</p>';
  }

  // ── Insights ──
  reportInsights.innerHTML = `<h3>Coach's Analysis</h3><p>${data.insights || '—'}</p>`;

  // ── Plan cards ──
  if (data.plan?.length) {
    const cards = data.plan.map(day => {
      const isRest = day.focus?.toLowerCase().includes('rest') || !day.exercises?.length;
      const exList = (day.exercises || []).join('<br>');
      return `<div class="plan-card ${isRest ? 'rest' : ''}">
        <div class="plan-day">${day.day}</div>
        <div class="plan-focus">${day.focus || 'Rest'}</div>
        <div class="plan-exs">${exList || '—'}</div>
        ${day.notes ? `<div class="plan-note">${day.notes}</div>` : ''}
      </div>`;
    }).join('');
    reportPlan.innerHTML = '<h3>Next Week\'s Plan</h3><div class="plan-grid">' + cards + '</div>';
  } else {
    reportPlan.innerHTML = '<h3>Next Week\'s Plan</h3><p style="color:#666">Plan unavailable.</p>';
  }

  reportLoading.classList.add('hidden');
  reportContent.classList.remove('hidden');
}

reportBtn.addEventListener('click', openReportModal);
reportClose.addEventListener('click', closeReportModal);
document.getElementById('report-backdrop').addEventListener('click', closeReportModal);

// ── Tab switching ─────────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.add('hidden'));
    btn.classList.add('active');
    document.getElementById(btn.dataset.tab).classList.remove('hidden');
    if (btn.dataset.tab === 'reanalyze-tab') loadSessionList();
  });
});

// ── Re-analyze tab ────────────────────────────────────────────────────────────
const sessionListEl       = document.getElementById('session-list');
const sessionListLoading  = document.getElementById('session-list-loading');
const reanalyzeProgressWrap = document.getElementById('reanalyze-progress-wrap');
const reanalyzeLabel      = document.getElementById('reanalyze-progress-label');
const reanalyzeBar        = document.getElementById('reanalyze-bar');
const reanalyzeResult     = document.getElementById('reanalyze-result');

let _sessionsLoaded = false;

function loadSessionList() {
  if (_sessionsLoaded) return;
  sessionListLoading.classList.remove('hidden');
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'list_sessions' }));
  }
}

function renderSessionList(sessions) {
  _sessionsLoaded = true;
  sessionListLoading.classList.add('hidden');
  if (!sessions.length) {
    sessionListEl.innerHTML = '<p style="color:#666;font-size:.83rem">No recorded sessions found. Sessions with video recordings will appear here.</p>';
    return;
  }
  sessionListEl.innerHTML = sessions.map(s => `
    <div class="session-card">
      <div class="session-info">
        <div class="session-date">${s.date}</div>
        <div class="session-time">${s.start} → ${s.end}</div>
      </div>
      <button onclick="startReanalyze(${s.session_id}, '${s.video_path}', this)">
        Analyze
      </button>
    </div>`).join('');
}

function startReanalyze(sessionId, videoPath, btn) {
  btn.disabled = true;
  btn.textContent = 'Running…';
  reanalyzeProgressWrap.classList.remove('hidden');
  reanalyzeResult.classList.add('hidden');
  reanalyzeBar.style.width = '0%';
  reanalyzeLabel.textContent = 'Starting…';
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'reanalyze_session', session_id: sessionId, video_path: videoPath }));
  }
}

function renderReanalyzeResult(data) {
  reanalyzeProgressWrap.classList.add('hidden');
  reanalyzeResult.classList.remove('hidden');

  const exRows = data.exercises.length
    ? data.exercises.map(e => `
        <div class="re-ex-row">
          <span>${e.name}</span>
          <span class="re-ex-count">${e.count} clip${e.count !== 1 ? 's' : ''}</span>
        </div>`).join('')
    : '<p style="color:#666">No exercises identified.</p>';

  const durMin = Math.round(data.duration_s / 60);
  reanalyzeResult.innerHTML = `
    <h4>Found ${data.exercises.length} exercise${data.exercises.length !== 1 ? 's' : ''} in ${data.clips_analyzed} clips (${durMin} min video)</h4>
    ${exRows}`;

  // Re-enable all analyze buttons
  document.querySelectorAll('.session-card button').forEach(b => {
    b.disabled = false;
    b.textContent = 'Analyze';
  });
  log.info(`Reanalysis done — ${data.exercises.length} exercises, ${data.clips_analyzed} clips`);
}

// ── Boot ──────────────────────────────────────────────────────────────────────
log.info('FormCheck renderer initialising');
connect();
