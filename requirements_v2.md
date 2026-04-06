# FormCheck Feature Additions — Video Storage + Post-Session Analysis

## Project context
FormCheck is a macOS Electron + Python asyncio app. Python backend lives in `backend/`,
Electron renderer in `renderer/`. WebSocket on port 8765.

Current relevant files:
- backend/server.py   — asyncio WebSocket server with camera_loop + analysis_loop
- backend/analyzer.py — Ollama vision calls, currently returns FeedbackResult(exercise, issues, severity, tip)
- backend/camera.py   — Camera class: get_frame(), get_annotated_frame(proportions), get_landmarks(frame)
- backend/journal.py  — SQLite: sessions(id, start_time, end_time, summary), events(id, session_id, ts, exercise, severity, issue, tip)
- backend/logger.py   — get_logger(__name__) used by all modules
- renderer/index.html / app.js — Electron UI

recorder.py does NOT exist yet. recordings/ directory does NOT exist yet.

Read the existing files before making any changes — do not guess at their content.

## Feature 1 — Session video recording (max 7 files)

### New file: backend/recorder.py

Create class SessionRecorder. Use `from logger import get_logger` and `log = get_logger(__name__)`.

```
__init__(self, width: int, height: int, fps: int = 15) -> None
```
- RECORDINGS_DIR = Path(__file__).parent / "recordings"
- MAX_RECORDINGS = 7
- Create RECORDINGS_DIR if absent (mkdir parents=True, exist_ok=True)
- self._writer = None, self._video_path = None
- Store width, height, fps as attributes
- Log "SessionRecorder initialised — size=%dx%d fps=%d recordings_dir=%s"

```
start(self) -> Path
```
- If self._writer is not None: raise RuntimeError("Recording already active")
- filename = f"session_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.mp4"
- path = RECORDINGS_DIR / filename
- fourcc = cv2.VideoWriter_fourcc(*"mp4v")
- self._writer = cv2.VideoWriter(str(path), fourcc, self.fps, (self.width, self.height))
- self._video_path = path
- Log "Recording started — path=%s"
- Return path

```
write_frame(self, frame: np.ndarray) -> None
```
- If self._writer is not None: self._writer.write(frame)
- Else: no-op silently

```
stop(self) -> Path | None
```
- If self._writer is None: return None
- self._writer.release(); self._writer = None
- path = self._video_path; self._video_path = None
- Log "Recording stopped — path=%s"
- Call self._enforce_limit()
- Return path

```
_enforce_limit(self) -> None
```
- files = sorted(RECORDINGS_DIR.glob("session_*.mp4"), key=lambda p: p.stat().st_mtime)
- while len(files) > MAX_RECORDINGS: files[0].unlink(); log info "Deleted old recording: %s"; files.pop(0)

### Modified: backend/journal.py

In init_db(), after the CREATE TABLE IF NOT EXISTS for sessions, add safe migration:
```python
try:
    conn.cursor().execute("ALTER TABLE sessions ADD COLUMN video_path TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass  # column already exists
```

Add new function:
```python
def update_session_video(session_id: int, video_path: str) -> None:
    conn = _conn()
    c = conn.cursor()
    c.execute("UPDATE sessions SET video_path=? WHERE id=?", [video_path, session_id])
    conn.commit()
    conn.close()
```

### Modified: backend/server.py — video wiring

Add imports: `from recorder import SessionRecorder`, `import math`
Update journal import to include: `from journal import init_db, start_session, log_event, end_session, update_session_video`

In handle_client(), add to state dict:
```python
state["recorder"] = None
state["angle_series"] = []
```

In camera_loop():
- Add frame counter variable `_frame_counter = 0` before the while loop
- Inside the loop, increment `_frame_counter += 1` each iteration
- After successfully encoding and sending a frame, add:
  ```python
  if _frame_counter % 2 == 0 and state.get("active") and state.get("recorder"):
      state["recorder"].write_frame(frame)
  ```
  (write the annotated frame — the same `frame` variable used for encoding)

In start_session handler, after setting state["active"] = True:
```python
state["angle_series"] = []
width  = int(camera.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
height = int(camera.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
state["recorder"] = SessionRecorder(width=width, height=height, fps=15)
state["recorder"].start()
slog.info("Recording started for session_id=%d", sid)
```

In stop_session handler, after setting state["active"] = False, replace the existing end_session call with this sequence:

```
# Step 1 — stop recording
recorder = state.get("recorder")
video_path = recorder.stop() if recorder is not None else None
state["recorder"] = None
slog.info("Recording stopped — video_path=%s", video_path)

# Step 2 — update DB
if sid is not None:
    end_session(sid, summary="Session ended by user")
if video_path is not None and sid is not None:
    update_session_video(sid, str(video_path))

# Step 3 — sample keyframes, run summary, count reps, send session_summary
# (see Feature 2 stop_session logic below)
```

## Feature 2 — Form-only during session; exercise ID + rep count post-session

### Modified: backend/analyzer.py

Read the current file first. Then:

REMOVE: FeedbackResult dataclass and analyze() method.

ADD dataclasses:
```python
@dataclass
class FormCheckResult:
    issues: list = field(default_factory=list)
    severity: str = "OK"
    tip: str = ""

@dataclass
class SummaryResult:
    exercises: list = field(default_factory=list)  # list of dicts: {"name": str, "estimated_reps": int}
    total_reps: int = 0
```

ADD method `analyze_form(self, frame) -> FormCheckResult`:
- Same JPEG+base64 encoding as existing code
- System prompt (exact text):
  ```
  You are a form coach. Check only posture and alignment.
  Return JSON: {"issues":[], "severity":"OK", "tip":""}.
  Severity: OK=correct form, WARNING=visible deviation, CRITICAL=injury risk.
  Be critical — flag any misalignment you can see.
  ```
- User message: "Check the form in this exercise frame and reply with JSON only."
- Parse JSON from response. On JSONDecodeError or any Exception: return FormCheckResult()
- Log the result at INFO level: "analyze_form #%d — severity=%s issues=%s latency=%.1fs"
- If severity is WARNING or CRITICAL: log at WARNING level

ADD method `analyze_summary(self, frames: list) -> SummaryResult`:
- If frames is empty: return SummaryResult() immediately (no Ollama call)
- Encode each frame as base64 JPEG (same encoding pattern)
- Single ollama.chat() call passing all base64 images in the images list
- System prompt (exact text):
  ```
  You are a fitness coach reviewing exercise frames from a completed session.
  Identify what exercises were performed. For each exercise, estimate how many
  total reps were completed during the session.
  Return JSON only: {"exercises":[{"name":"Push-Up","estimated_reps":12}]}
  ```
- User message: "Review these frames from a completed workout session and reply with JSON only."
- Parse JSON. total_reps = sum of estimated_reps across all exercises.
- On any failure: return SummaryResult()
- Log result at INFO level: "analyze_summary — exercises=%s total_reps=%d latency=%.1fs"

### Module-level helpers in backend/server.py

Add these two functions at module level (before camera_loop):

```python
def _joint_angle(lm_list, a: int, b: int, c: int) -> float:
    """Angle in degrees at joint b, given landmarks a-b-c."""
    A = (lm_list[a].x, lm_list[a].y)
    B = (lm_list[b].x, lm_list[b].y)
    C = (lm_list[c].x, lm_list[c].y)
    BA = (A[0] - B[0], A[1] - B[1])
    BC = (C[0] - B[0], C[1] - B[1])
    dot = BA[0] * BC[0] + BA[1] * BC[1]
    mag_ba = math.sqrt(BA[0] ** 2 + BA[1] ** 2)
    mag_bc = math.sqrt(BC[0] ** 2 + BC[1] ** 2)
    if mag_ba == 0 or mag_bc == 0:
        return 180.0
    cosine = max(-1.0, min(1.0, dot / (mag_ba * mag_bc)))
    return math.degrees(math.acos(cosine))


def _count_reps(angle_series: list) -> int:
    """Count rep cycles from a joint angle time series.

    A rep cycle = angle crosses below LOW_THRESHOLD then above HIGH_THRESHOLD
    (or vice versa). Returns number of complete cycles (transitions // 2).
    """
    LOW, HIGH = 110.0, 150.0
    if not angle_series:
        return 0
    state = None
    transitions = 0
    for angle in angle_series:
        if state is None:
            if angle < LOW:
                state = "low"
            elif angle > HIGH:
                state = "high"
        elif state == "low" and angle > HIGH:
            transitions += 1
            state = "high"
        elif state == "high" and angle < LOW:
            transitions += 1
            state = "low"
    return transitions // 2
```

Add helper function `_sample_keyframes` at module level:
```python
def _sample_keyframes(video_path, interval_s: int = 30, max_frames: int = 5) -> list:
    """Sample up to max_frames frames from video_path, one every interval_s seconds."""
    import cv2 as _cv2
    cap = _cv2.VideoCapture(str(video_path))
    try:
        fps   = cap.get(_cv2.CAP_PROP_FPS)
        total = int(cap.get(_cv2.CAP_PROP_FRAME_COUNT))
        if fps <= 0 or total <= 0:
            return []
        indices = [int(i * interval_s * fps) for i in range(max_frames)
                   if int(i * interval_s * fps) < total]
        frames = []
        for idx in indices:
            cap.set(_cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if ret:
                frames.append(frame)
        return frames
    finally:
        cap.release()
```

### Modified: backend/server.py — analysis_loop

Replace `analyzer.analyze(frame)` with `analyzer.analyze_form(frame)`.
`analyze_form` returns `FormCheckResult` with fields: issues, severity, tip.

After `raw_frame = camera.get_frame()` (before dispatching to executor), add landmark tracking:
```python
try:
    landmarks = camera.get_landmarks(raw_frame)
    if landmarks is not None:
        for (a, b, c) in [(11, 13, 15), (12, 14, 16), (23, 25, 27), (24, 26, 28)]:
            state["angle_series"].append(_joint_angle(landmarks, a, b, c))
except Exception as _lm_err:
    alog.debug("Landmark angle tracking failed: %s", _lm_err)
```

Update WebSocket message — remove "exercise" field:
```python
await websocket.send(json.dumps({
    "type":      "analysis",
    "severity":  result.severity,
    "issues":    result.issues,
    "tip":       result.tip,
    "latency_s": round(latency, 1),
}))
```

Update log lines that reference result.exercise or result.reps to remove those fields.

### Modified: backend/server.py — stop_session post-session logic

After Steps 1 and 2 (recording stop + DB update), add Steps 3-6:

```python
# Step 3 — sample keyframes
keyframes = []
if video_path is not None:
    try:
        keyframes = _sample_keyframes(video_path)
        slog.info("Sampled %d keyframes from %s", len(keyframes), video_path)
    except Exception as _kf_err:
        slog.warning("Keyframe sampling failed: %s", _kf_err)

# Step 4 — post-session Ollama summary (best-effort; don't block the message)
summary_result = None
if keyframes:
    try:
        loop = asyncio.get_running_loop()
        summary_result = await loop.run_in_executor(None, analyzer.analyze_summary, keyframes)
        slog.info("Post-session summary: exercises=%s total_reps=%d",
                  [e["name"] for e in summary_result.exercises], summary_result.total_reps)
    except Exception as _sum_err:
        slog.warning("analyze_summary failed: %s — sending empty summary", _sum_err)

# Step 5 — count angle-based reps
angle_reps = _count_reps(state["angle_series"])
slog.info("Angle-based rep count: %d (from %d angle samples)", angle_reps, len(state["angle_series"]))

# Step 6 — send session_summary (always send, even if Ollama failed)
exercises = []
if summary_result is not None:
    seen = set()
    for e in summary_result.exercises:
        name = e.get("name", "")
        if name and name not in seen:
            exercises.append(name)
            seen.add(name)

await websocket.send(json.dumps({
    "type":       "session_summary",
    "exercises":  exercises,
    "total_reps": angle_reps,
}))
slog.info("session_summary sent — exercises=%s total_reps=%d", exercises, angle_reps)

# Step 7 — reset angle series
state["angle_series"] = []
```

### Modified: renderer/index.html

After the closing `</table>` tag of the journal table (id="journal-table"), add:
```html
<div id="session-summary" style="display:none;">
  <hr>
  <h3>Session Summary</h3>
  <p id="summary-exercises">—</p>
  <p id="summary-reps">—</p>
</div>
```

### Modified: renderer/app.js

1. Add case for "session_summary" in the WebSocket message switch:
```javascript
case 'session_summary': {
  const summaryDiv = document.getElementById('session-summary');
  summaryDiv.style.display = 'block';
  document.getElementById('summary-exercises').textContent =
    'Exercises: ' + (msg.exercises.length ? msg.exercises.join(', ') : 'Unknown');
  document.getElementById('summary-reps').textContent =
    'Total Reps: ' + msg.total_reps;
  // Add summary row to journal
  const tr = document.createElement('tr');
  const ts = new Date().toLocaleTimeString();
  tr.innerHTML = `<td>${ts}</td><td>${msg.exercises.join(', ') || 'Unknown'}</td><td>Summary</td>`;
  journalBody.prepend(tr);
  log.info('Session summary received — exercises=' + msg.exercises.join(',') +
           ' total_reps=' + msg.total_reps);
  break;
}
```

2. In the "analysis" case, remove any reference to msg.exercise (it no longer exists in the message).

3. In the start_session button click handler, after sending the WebSocket message add:
```javascript
document.getElementById('session-summary').style.display = 'none';
```

Also log the session_summary at INFO level in the logAnalysis function equivalent.

## Implementation order
1. backend/journal.py — no dependencies on new code
2. backend/recorder.py — no project dependencies
3. backend/analyzer.py — remove FeedbackResult/analyze(), add FormCheckResult/SummaryResult/analyze_form/analyze_summary
4. backend/server.py — depends on all above; add _joint_angle, _count_reps, _sample_keyframes helpers
5. renderer/index.html — add session-summary div
6. renderer/app.js — wire session_summary handler

## Acceptance criteria
1. backend/recorder.py exists; SessionRecorder.start() creates MP4 in backend/recordings/
2. recordings/ directory auto-created on first start() call
3. After 8 sessions recorded, only 7 MP4 files exist in recordings/ (oldest deleted)
4. sessions.video_path populated in SQLite after stop_session
5. During-session "analysis" WebSocket messages have no "exercise" field
6. After stop_session, client receives "session_summary" with exercises (list of strings) and total_reps (int >= 0)
7. total_reps is 0 if no landmarks detected during session — no exception raised
8. If video absent or unreadable, exercises=[] — no exception raised
9. index.html #session-summary is hidden at load; becomes visible after session_summary received
10. start_session hides the summary div
11. Existing DB data and columns unaffected by video_path migration
12. Python syntax check passes for all modified files
```
