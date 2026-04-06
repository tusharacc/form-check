# FormCheck — Real-time Exercise Posture Coach

## Project overview
Desktop app (macOS, Electron + Python) that uses the MacBook webcam to monitor exercise
sessions. The app identifies the exercise being performed, overlays a pose skeleton on the
live video feed, delivers posture corrections in real time (on-screen + voice + sound), and
keeps a session journal in SQLite.

## Tech stack (do not deviate)
- Electron 30 + Node 20 (desktop shell)
- Python 3.11 backend (WebSocket server on port 8765)
- OpenCV 4.x + MediaPipe 0.10.x for camera and skeleton overlay
- ollama Python SDK — llama3.2-vision:11b for exercise analysis
- websockets 12 (asyncio) for Python ↔ Electron IPC
- macOS `say` via subprocess for TTS
- pygame 2.x for alert sounds
- sqlite3 (stdlib) for journal

## File structure to produce
formcheck/
├── package.json
├── main.js
├── preload.js
├── renderer/
│   ├── index.html
│   ├── style.css
│   └── app.js
├── backend/
│   ├── pyproject.toml
│   ├── server.py
│   ├── camera.py
│   ├── calibrator.py
│   ├── analyzer.py
│   ├── voice.py
│   ├── sound.py
│   └── journal.py
└── assets/
    └── (alert.wav — generate a short beep programmatically in sound.py if file absent)

## Module specifications

### main.js
- Creates BrowserWindow (1200×800, dark background)
- On ready: spawn Python `backend/server.py` as child process
- On window close: kill Python child process
- Load renderer/index.html

### preload.js
- Expose `window.api` with ipcRenderer send/on wrappers

### renderer/index.html + style.css + app.js
- Dark theme (#1a1a1a background, #e0e0e0 text)
- Left pane: <canvas> element showing video feed (640×480)
- Right pane: exercise name, rep count, posture issue list (coloured by severity: green/orange/red),
  coaching tip, session timer
- Calibration screen shown first: "Stand upright, arms at sides" instruction + progress bar
- Bottom tabs: [Live] [Journal]
- Journal tab: table of past sessions (date, exercises, duration, issues count)
- app.js: WebSocket client connects to ws://localhost:8765
  - On "calibrating" message: show calibration screen with progress bar
  - On "calibration_done" message: hide calibration screen, show live session UI
  - On "frame" message: draw base64 JPEG to canvas
  - On "analysis" message: update sidebar state (exercise, reps, issues, severity, tip)
  - Start/Stop buttons send {"type":"start_session"} / {"type":"stop_session"}

### backend/server.py
- asyncio WebSocket server on 0.0.0.0:8765
- On client connect:
  1. Send {"type":"calibrating"} to Electron (show calibration screen)
  2. Run Calibrator.run(camera) — streams frames during calibration so user sees live skeleton
  3. On success send {"type":"calibration_done"}; on CalibrationError send {"type":"calibration_failed","reason":"..."}
  4. Start camera loop and analysis loop as asyncio tasks
- Camera loop: calls camera.get_annotated_frame(proportions) → encodes JPEG → sends {"type":"frame","data":"<b64>"}
- Analysis loop: every 5 seconds, calls analyzer.analyze(raw_frame) → sends {"type":"analysis",...}
  and triggers voice.speak(tip) and sound.alert(severity)
- Handles start_session / stop_session messages → calls journal functions
- On client disconnect: cancel tasks, close camera

### backend/calibrator.py
- Class Calibrator with run(camera) -> BodyProportions
- Collects 150 frames (~5 seconds at 30fps) while user stands in neutral pose
- Uses camera.get_landmarks() to get raw MediaPipe landmarks per frame
- Filters frames where all 11 upper-body landmark confidences > 0.6
- Averages valid frames to produce BodyProportions dataclass:
  shoulder_width, torso_height, left_arm_len, right_arm_len,
  left_leg_len, right_leg_len — all as ratios of frame height (scale-invariant)
- If fewer than 30 valid frames captured, raises CalibrationError

### backend/camera.py
- Class Camera with open() / close() / get_frame() / get_annotated_frame(proportions=None) / get_landmarks()
- Uses cv2.VideoCapture(0)
- MediaPipe Selfie Segmentation initialized (model_selection=1 — landscape)
- MediaPipe Pose initialized with model_complexity=2, min_detection_confidence=0.6
- Per frame pipeline:
  1. Selfie segmentation → binary mask
  2. Apply mask (set background pixels to dark grey #222222)
  3. Run MediaPipe Pose on masked frame
  4. Skip draw if any key joint (shoulders, hips, knees) confidence < 0.5
- get_annotated_frame(proportions): draw pose landmarks (connections cyan, joints white dots)
  Returns JPEG bytes quality=70
- get_frame(): returns raw BGR numpy array (before masking) — used by analyzer
- get_landmarks(): returns raw MediaPipe landmark list — used by calibrator

### backend/analyzer.py
- Class Analyzer with analyze(frame: np.ndarray) -> FeedbackResult
- FeedbackResult dataclass: exercise(str), reps(int|None), issues(list[str]), severity(str), tip(str)
- Encodes frame as JPEG → base64
- Calls ollama.chat() with model="llama3.2-vision:11b", passes image as base64 in message content
- System prompt: "You are a professional fitness coach analyzing a single webcam frame.
  Respond ONLY with a JSON object — no prose, no markdown:
  {\"exercise\": \"<name or resting>\", \"reps\": <int or null>, \"issues\": [\"...\"],
  \"severity\": \"OK\"|\"WARNING\"|\"CRITICAL\", \"tip\": \"<one actionable coaching cue>\"}"
- Parse JSON from response content; on parse failure return FeedbackResult(exercise="unknown",
  reps=None, issues=[], severity="OK", tip="")
- Request timeout: 30 seconds

### backend/voice.py
- Class VoiceEngine with speak(text: str)
- Non-blocking: subprocess.Popen(["say", text])
- Rate limit: only speak if 10+ seconds since last utterance
- speak() is a no-op if text is empty

### backend/sound.py
- Class SoundEngine with alert(severity: str)
- Uses pygame.mixer
- severity "WARNING" → short single beep (440Hz, 0.3s)
- severity "CRITICAL" → double beep (880Hz, 0.2s x2)
- severity "OK" → silence
- Generate beep using numpy sine wave if pygame cannot load a .wav file

### backend/journal.py
- Functions: init_db(db_path), start_session() -> int,
  log_event(session_id, exercise, reps, severity, issue, tip),
  end_session(session_id, summary), get_sessions() -> list[dict]
- DB path: ~/formcheck_journal.db
- Schema:
  CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, start_time TEXT NOT NULL, end_time TEXT, summary TEXT);
  CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER REFERENCES sessions(id), ts TEXT NOT NULL, exercise TEXT, reps INTEGER, severity TEXT, issue TEXT, tip TEXT);

## Acceptance criteria
1. `npm start` launches app and Python backend starts within 3 seconds
2. Calibration screen shown on launch — user holds still for 5 seconds, progress bar updates
3. After calibration, video feed renders at ≥10fps with selfie segmentation (background dimmed)
4. Skeleton overlay drawn only when key joint confidence ≥ 0.5
5. After 5 seconds of exercise, first analysis result (exercise name + reps) appears in sidebar
6. WARNING severity → orange text + voice tip spoken by macOS say + single beep
7. CRITICAL severity → red text + voice tip + double beep
8. Stop button saves session to SQLite, journal tab updates with new row
9. Closing window kills Python child process (no orphan processes)
10. All network calls go to localhost:11434 only (Ollama)
