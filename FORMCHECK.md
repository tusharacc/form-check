# FormCheck — Project Context

Read this before starting any new Claude session on this project.
It captures everything built so far, why decisions were made, and what to watch out for.

---

## What FormCheck is

A real-time AI exercise form coach running entirely on a local M3 Mac.

- **Live video feed** from webcam with background segmentation (person isolated on dark bg)
- **Pose skeleton overlay** via MediaPipe (shoulder, elbow, wrist, hip, knee, ankle joints)
- **Form analysis** every 45 s during a session — Ollama vision model flags posture issues
- **Rep counting** from joint angle tracking (elbow + knee angle time series)
- **Post-session summary** — exercise identification + rep count after the session ends
- **Video recording** — session MP4 saved automatically (max 7 files, oldest auto-deleted)
- **SQLite journal** — every session and analysis event persisted to `backend/formcheck.db`
- **Sound alerts** — pygame beeps on WARNING (440 Hz) and CRITICAL (880 Hz) detections

---

## Tech stack

| Layer | Technology |
|---|---|
| Desktop shell | Electron 30 (Node 20) |
| Renderer | Vanilla HTML/JS (no framework) |
| IPC | WebSocket — Python server at `ws://127.0.0.1:8765` |
| Pose estimation | MediaPipe Tasks API `PoseLandmarker` (full model, `.task` file) |
| Vision AI | Ollama `llama3.2-vision:11b` — runs on M3 Metal GPU (~40–55 s/call) |
| Video | OpenCV `cv2` — capture, annotation, VideoWriter (mp4v codec) |
| Audio | pygame mixer — sine wave fallback when no WAV file present |
| DB | SQLite3 via stdlib `sqlite3` |
| Logging | Python `logging.handlers.RotatingFileHandler` — `backend/logs/formcheck.log` |

---

## Directory layout

```
formcheck/
├── main.js               Electron main process — BrowserWindow, no nodeIntegration
├── preload.js            contextBridge (currently empty — direct WS from renderer)
├── package.json          Electron app config, start script
├── renderer/
│   ├── index.html        UI layout — calibration overlay, canvas, sidebar, journal, summary
│   └── app.js            All frontend logic — WebSocket client, canvas draw, session control
├── backend/
│   ├── server.py         asyncio WebSocket server — 3 concurrent tasks per client
│   ├── analyzer.py       Ollama vision calls — FormCheckResult + SummaryResult
│   ├── calibrator.py     Neutral-pose calibration — BodyProportions from 150 frames
│   ├── camera.py         OpenCV capture + MediaPipe PoseLandmarker Tasks API
│   ├── recorder.py       SessionRecorder — MP4 writer, 7-file rotation
│   ├── journal.py        SQLite — sessions + events tables, init_db + CRUD helpers
│   ├── sound.py          SoundEngine — pygame beeps, _generate_sine_wave fallback
│   ├── logger.py         get_logger() — rotating file handler to logs/formcheck.log
│   ├── voice.py          VoiceEngine (stub/placeholder — not actively used)
│   ├── pyproject.toml    Python dependencies (websockets, opencv, mediapipe, ollama, pygame)
│   ├── pose_landmarker_full.task   MediaPipe model file (9.3 MB, auto-downloaded)
│   ├── formcheck.db      SQLite database (auto-created on first run)
│   ├── logs/
│   │   ├── formcheck.log         Active log (10 MB max, then rotated)
│   │   └── formcheck.log.1       Previous log (up to 5 backups kept)
│   └── recordings/       MP4 session videos (auto-created, max 7 files)
├── requirements.md       Original feature requirements doc
├── requirements_v2.md    Updated requirements (recording + post-session summary)
└── ca_memory/            ca (code-assistant) pipeline memory — do not edit manually
```

---

## How to start the app

### 1. Start the Python backend
```bash
cd /Users/tusharsaurabh/Documents/Projects/Python/formcheck/backend
python server.py
```
The server prints nothing to stdout — all output goes to `logs/formcheck.log`.
Wait for this log line before starting Electron:
```
INFO  formcheck.session  Entering message loop — ready for start_session / stop_session commands
```

### 2. Start Electron (separate terminal)
```bash
cd /Users/tusharsaurabh/Documents/Projects/Python/formcheck
npm start    # runs "electron ." — this is the correct way
```

### CRITICAL: always restart the backend after changing Python files
Python loads the module once at startup. Editing `server.py`, `analyzer.py`, etc. has
zero effect until you `Ctrl+C` the backend and run `python server.py` again.

---

## Session lifecycle (end-to-end)

```
[Electron starts]
    → app.js opens WebSocket to ws://127.0.0.1:8765
    → server receives connection → starts camera_loop + analysis_loop + handle_client tasks

[Calibration phase]
    → server sends {"type": "calibrating"}
    → Calibrator.run_async() collects 150 frames, needs ≥30 valid upper-body frames
    → server sends {"type": "calibration_done"} or {"type": "calibration_failed", "reason": "..."}
    → camera_loop streams annotated frames as {"type": "frame", "data": "<base64 JPEG>"}

[User clicks Start Session]
    → app.js sends {"type": "start_session"}
    → server: creates DB session row, starts SessionRecorder, sets state["active"]=True
    → server sends {"type": "session_started", "session_id": 5}
    → analysis_loop wakes every 45 s → dispatches analyze_form() in executor
    → server sends {"type": "analysis", "severity": "WARNING", "issues": [...], "tip": "...", "latency_s": 53.2}
    → camera_loop writes every other frame to SessionRecorder (15 fps recording)
    → server tracks joint angles → angle_series list grows with each frame

[User clicks Stop Session]
    → app.js sends {"type": "stop_session"}
    → server (7-step stop flow):
        1. state["active"] = False  (stops analysis_loop from dispatching new calls)
        2. recorder.stop() → saves MP4 to recordings/, enforces 7-file limit
        3. end_session(sid) + update_session_video(sid, path) → DB updated
        4. _sample_keyframes(video_path) → up to 5 frames, 1 per 30 s of video
        5. analyzer.analyze_summary(keyframes) in executor → SummaryResult
        6. _count_reps(angle_series) → integer rep count from joint angles
        7. websocket.send({"type": "session_summary", "exercises": [...], "total_reps": 12})
    → app.js shows session-summary div with exercise list + rep count
    → app.js adds journal row labelled "Summary"
```

---

## WebSocket protocol

### Server → Client

| Message type | When sent | Payload |
|---|---|---|
| `calibrating` | On connection | `{}` |
| `calibration_done` | Calibration succeeds | `{}` |
| `calibration_failed` | Calibration fails | `{"reason": "Only 12 usable frames..."}` |
| `frame` | Every ~33 ms (camera_loop) | `{"data": "<base64 JPEG>"}` |
| `session_started` | After `start_session` | `{"session_id": 5}` |
| `analysis` | Every 45 s during session | `{"severity": "OK"\|"WARNING"\|"CRITICAL", "issues": [...], "tip": "...", "latency_s": 53.2}` |
| `session_summary` | After `stop_session` | `{"exercises": ["Push-Up", "Squat"], "total_reps": 12}` |

### Client → Server

| Message type | When sent | Payload |
|---|---|---|
| `start_session` | Start button click | `{}` |
| `stop_session` | Stop button click | `{}` |

---

## Key server constants (`backend/server.py`)

```python
ANALYSIS_INTERVAL = 45    # seconds between form checks (matches ~40 s M3 Metal latency)
WS_HOST           = "0.0.0.0"
WS_PORT           = 8765
DB_PATH           = "backend/formcheck.db"

# Rep counting thresholds (in degrees)
LOW_THRESHOLD  = 110.0    # joint fully bent (e.g. bottom of a squat)
HIGH_THRESHOLD = 150.0    # joint extended (e.g. standing)
# Complete rep = cross LOW → HIGH → LOW (or HIGH → LOW → HIGH)
# Reps = transitions // 2

# Joints tracked for angle series (MediaPipe landmark indices)
# LEFT_ELBOW  = (11, 13, 15)   shoulder → elbow → wrist
# RIGHT_ELBOW = (12, 14, 16)
# LEFT_KNEE   = (23, 25, 27)   hip → knee → ankle
# RIGHT_KNEE  = (24, 26, 28)
```

---

## Key recorder constants (`backend/recorder.py`)

```python
RECORDINGS_DIR = Path(__file__).parent / "recordings"   # backend/recordings/
MAX_RECORDINGS = 7                                        # oldest deleted when exceeded
FPS            = 15                                       # recording fps (every other camera frame)
FOURCC         = "mp4v"                                   # H.265-compatible codec
```

---

## Database schema (`backend/formcheck.db`)

```sql
CREATE TABLE sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time  TEXT NOT NULL,       -- ISO-8601 UTC
    end_time    TEXT,                -- NULL until stop_session
    summary     TEXT,                -- "Session ended by user"
    video_path  TEXT                 -- absolute path to MP4 (added via migration)
);

CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES sessions(id),
    ts          TEXT NOT NULL,       -- ISO-8601 UTC
    exercise    TEXT,                -- "Lunge", "Push-Up" etc. (title-cased)
    severity    TEXT,                -- "OK" | "WARNING" | "CRITICAL"
    issue       TEXT,                -- "right knee caving inward"
    tip         TEXT                 -- coaching advice string
);
```

---

## Ollama model configuration (`backend/analyzer.py`)

```python
_MODEL = "llama3.2-vision:11b"   # runs on M3 Metal GPU

# Form analysis prompt (during session — single frame)
_FORM_SYSTEM = (
    "You are a form coach. Check only posture and alignment.\n"
    "Return JSON: {\"issues\":[], \"severity\":\"OK\", \"tip\":\"\"}.\n"
    "Severity: OK=correct form, WARNING=visible deviation, CRITICAL=injury risk.\n"
    "Be critical — flag any misalignment you can see."
)

# Summary prompt (post-session — up to 5 keyframes)
_SUMMARY_SYSTEM = (
    "You are a fitness coach reviewing exercise frames from a completed session.\n"
    "Identify what exercises were performed. For each exercise, estimate how many\n"
    "total reps were completed during the session.\n"
    "Return JSON only: {\"exercises\":[{\"name\":\"Push-Up\",\"estimated_reps\":12}]}"
)
```

Expected JSON responses:
```json
// analyze_form
{"issues": ["right knee caving inward"], "severity": "WARNING", "tip": "Keep your knee over your toes."}

// analyze_summary
{"exercises": [{"name": "Lunge", "estimated_reps": 12}, {"name": "Squat", "estimated_reps": 8}]}
```

---

## Calibration (`backend/calibrator.py`)

- Collects 150 webcam frames, needs ≥30 with high-confidence upper-body landmarks
- Upper-body landmark indices: `(11, 12, 13, 14, 15, 16, 23, 24)` — shoulders, elbows, wrists, hips
- Visibility threshold: 0.6 per landmark
- Produces `BodyProportions` — normalised body ratios (shoulder width, torso height, arm/leg lengths)
- `run_async()` yields every 3 frames so camera_loop keeps streaming during calibration
- `CalibrationError` raised if not enough valid frames — shown to user as `calibration_failed`

---

## Logging

All log output goes to `backend/logs/formcheck.log`. Rotates at 10 MB, keeps 5 backups.

```bash
# Follow live logs
tail -f backend/logs/formcheck.log

# Find all analysis results
grep "analyze #" backend/logs/formcheck.log

# Find session start/stop events
grep "Session start\|Session stop\|session_summary" backend/logs/formcheck.log

# Find errors
grep "ERROR\|WARNING" backend/logs/formcheck.log | grep -v "Segmentation\|analysis_loop: no active"
```

Named loggers (all prefixed `formcheck.`):
- `formcheck.server` — top-level server startup/shutdown
- `formcheck.session` — per-message receive, start/stop session lifecycle
- `formcheck.analysis_loop` — analysis dispatch, latency, DB logging
- `formcheck.camera_loop` — FPS tracking, frame send counts
- `formcheck.camera` — segmentation mask, landmarks, FPS
- `formcheck.analyzer` — raw Ollama responses, parse results
- `formcheck.sound` — beep alerts

---

## Known issues and gotchas

### 1. Always restart the backend after code changes
The server is a long-running process. Edits to any `.py` file take no effect until
you `Ctrl+C` and `python server.py` again. The session you ran on Apr 3 (session 5)
showed no post-session summary because the server was started before the code was updated.

### 2. `sound.py` — `generate_sine_wave` was undefined (FIXED Apr 3)
The `play_beep()` fallback tried to call an undefined function when `assets/alert.wav`
was missing. This caused a `NameError` that silently killed the `analysis_loop` asyncio
task after the first WARNING/CRITICAL alert. Fixed by adding `_generate_sine_wave()` using
numpy. The pygame mixer is now explicitly initialised with matching sample rate (44100 Hz,
16-bit, stereo) so `sndarray.make_sound()` accepts the generated array.

### 3. MediaPipe segmentation mask shape
`result.segmentation_masks[0].numpy_view()` returns shape `(H, W, 1)` not `(H, W)`.
Always call `.squeeze()` before using it. This was a bug in an earlier version; camera.py
already handles it correctly with a shape check.

### 4. Ollama inference latency on M3
`llama3.2-vision:11b` takes **40–55 seconds** per call on M3 Metal GPU.
`ANALYSIS_INTERVAL = 45` was chosen to match this — the analysis_loop sleeps 45 s between
dispatches, so inference runs roughly back-to-back. Do not set this below 30 s.

### 5. Video recording uses every other frame
`camera_loop` writes frames to the recorder at `_frame_counter % 2 == 0` — half the
camera frames. With camera running at ~9.4 fps, this gives ~4.7 fps actual recording
rate stored at 15 fps (slightly sped up). Recordings are saved to `backend/recordings/`.

### 6. `_count_reps()` is angle-agnostic
The rep counter tracks the single joint with the largest angle swing across the session.
It cannot distinguish between arm and leg exercises. It counts complete LOW→HIGH→LOW
(or HIGH→LOW→HIGH) cycles in the angle_series. The vision model's `analyze_summary()`
provides the exercise name — the angle counter provides the rep count.

### 7. Post-session summary requires a video
If the session recorder failed to create an MP4 (e.g., codec issue, permissions),
`video_path` is None → no keyframes → `analyze_summary()` not called → `exercises = []`
in the summary. The `session_summary` message is still sent with `total_reps` from the
angle series. Check `backend/recordings/` to confirm video was created.

### 8. Calibration must succeed before Start Session is available
The Start Session button is enabled only after `calibration_done` is received.
If calibration fails (user not in frame, poor lighting), the reason is shown in the UI
and the user must reconnect (reload the page) to retry.

---

## Feature history (chronological)

| Date | What was built |
|---|---|
| Initial | Basic WebSocket server, OpenCV frame streaming, MediaPipe pose overlay |
| Early sessions | Calibration phase (BodyProportions), calibrator.py |
| Mar 30 | Ollama vision form analysis every 45 s (was 5 s → adjusted for real latency) |
| Mar 30 | SQLite journal — sessions + events tables |
| Mar 30 | Sound alerts — pygame beeps on WARNING/CRITICAL |
| Apr 2 | Background segmentation via MediaPipe output_segmentation_masks |
| Apr 2 | Extended logging across all modules (formcheck.* named loggers) |
| Apr 2–3 | Session video recording to MP4 (recorder.py, MAX_RECORDINGS=7) |
| Apr 2–3 | Post-session analysis: keyframe sampling → analyze_summary() → session_summary message |
| Apr 2–3 | Rep counting via joint angle time series (_count_reps, _joint_angle helpers) |
| Apr 2–3 | Two-mode analyzer: analyze_form() (single frame) + analyze_summary() (post-session) |
| Apr 3 | Fixed sound.py — added _generate_sine_wave() so analysis_loop no longer crashes |

---

## What does NOT exist yet

- Exercise selection / filtering (the user can't tell the app what exercise they're doing)
- Real-time rep counter display in the UI (only shown in post-session summary)
- Historical session review (past sessions not shown in UI, only current session journal)
- Export / share of session data
- Multi-person support (only 1 pose tracked)
- Voice feedback (voice.py exists but is a stub — not wired to anything)
- Threshold customisation (LOW=110°, HIGH=150° are hardcoded constants)

---

## Development tips

```bash
# Syntax check all backend files
python -m py_compile backend/*.py

# Test sound engine without running full server
cd backend && python -c "
import sound, time
s = sound.SoundEngine()
s.alert('WARNING')
time.sleep(1)
s.alert('CRITICAL')
time.sleep(1)
print('Sound test passed')
"

# Check DB state
cd backend && python -c "
import sqlite3
conn = sqlite3.connect('formcheck.db')
print(conn.execute('SELECT id, start_time, end_time, video_path FROM sessions ORDER BY id DESC LIMIT 5').fetchall())
print(conn.execute('SELECT session_id, exercise, severity, issue FROM events ORDER BY id DESC LIMIT 10').fetchall())
"

# Count recording files
ls -lh backend/recordings/

# Watch logs live
tail -f backend/logs/formcheck.log | grep -v "Segmentation mask\|person_pixels"
```
