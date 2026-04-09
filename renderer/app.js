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

// ── Boot ──────────────────────────────────────────────────────────────────────
log.info('FormCheck renderer initialising');
connect();
