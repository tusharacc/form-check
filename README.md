# FormCheck

A real-time exercise posture coach that runs entirely on your Mac — no cloud, no subscriptions. Your webcam feed never leaves the machine.

![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Electron](https://img.shields.io/badge/electron-30-47848f)

---

## What it does

| Feature | Detail |
|---|---|
| **Live pose overlay** | MediaPipe draws your skeleton on the camera feed in real time |
| **Form analysis** | Ollama (`llama3.2-vision:11b`) reviews your posture every 45 s and flags issues with severity OK / WARNING / CRITICAL |
| **Voice + sound alerts** | Critical form issues are spoken aloud and trigger an audio cue |
| **Rep counting** | Angle-based counter tracks elbow and knee joints at ~10 Hz; reports total reps at session end |
| **Session timer** | Counts up live while a session is active; freezes at the end showing total workout duration |
| **Screen stays on** | `caffeinate -d -i` keeps display and system awake for the entire session |
| **Apple Watch heart rate** | Polls HealthKit every 30 s during a session and shows live BPM in the sidebar |
| **Post-session summary** | Ollama identifies exercises from keyframes; angle data counts reps — both shown at session end |
| **Weekly Report** | Aggregates 30 days of history, categorises exercises by body part, and generates a personalised 5-day plan via Ollama |
| **Journal** | Every form event is logged to a local SQLite database with timestamp, exercise, severity, and coaching tip |

---

## Architecture

```
┌─────────────────────────────────┐
│         Electron (main.js)      │  ← manages window + Python lifecycle
│   BrowserWindow → renderer/     │
└──────────────┬──────────────────┘
               │ WebSocket  ws://127.0.0.1:8765
┌──────────────▼──────────────────┐
│      Python asyncio server      │  backend/server.py
│                                 │
│  camera_loop   — 30 fps JPEG stream
│  analysis_loop — Ollama form check every 45 s
│  heart_rate_loop — HealthKit poll every 30 s
│  handle_client — session control messages
└──┬──────┬──────┬──────┬─────────┘
   │      │      │      │
Camera  Analyzer Calibrator  Reporter
(cv2 +  (Ollama) (MediaPipe) (SQLite +
MediaPipe)                   Ollama plan)
```

**Stack**

- **Frontend** — Electron 30, vanilla JS/HTML/CSS (no framework)
- **Backend** — Python 3.11+, asyncio, websockets
- **Computer vision** — OpenCV, MediaPipe Tasks (PoseLandmarker)
- **AI** — Ollama `llama3.2-vision:11b` running locally on Metal
- **Storage** — SQLite (sessions + events journal)
- **macOS integrations** — HealthKit via pyobjc, caffeinate wake lock, Shortcuts for Watch notifications

---

## Requirements

- macOS 13+ (Apple Silicon recommended — GPU inference via Metal)
- [Ollama](https://ollama.com) with `llama3.2-vision:11b` pulled
- [Anaconda](https://www.anaconda.com) or any Python 3.11+ environment
- Node.js 18+
- Webcam
- Apple Watch (optional — for heart rate)

---

## Setup

### 1. Pull the Ollama model
```bash
ollama pull llama3.2-vision:11b
```

### 2. Install Python dependencies
```bash
cd backend
pip install websockets opencv-python mediapipe numpy ollama pygame \
            pyobjc-framework-HealthKit
```

### 3. Install Node dependencies
```bash
npm install
```

### 4. Grant camera access
macOS will prompt on first launch. If it doesn't, go to
**System Settings → Privacy & Security → Camera** and enable access for Terminal (or your Python environment).

---

## Running

### Option A — shell script (recommended)
```bash
npm start
# or directly:
bash start.sh
```
`start.sh` kills any stale backend on port 8765, starts the Python server, waits until it is ready, then launches the Electron window.

### Option B — Electron directly
```bash
npx electron .
```
`main.js` handles the same port-cleanup and readiness-wait logic, so this is equivalent.

---

## Usage

1. **Calibration** — on launch, stand in full view of the camera with your upper body visible. The calibration overlay runs for ~5 s and disappears automatically.
2. **Start Session** — click the green button. The timer starts. Ollama analyses your form every 45 s.
3. **Work out** — voice alerts fire on WARNING/CRITICAL form issues. Heart rate shows in the sidebar if Apple Watch is connected.
4. **Stop Session** — click the red button. A summary appears with detected exercises and total reps.
5. **Weekly Report** — click **📊 Weekly Report** any time to see 30-day history, body-part coverage chart, and a personalised next-week plan.

---

## Project structure

```
formcheck/
├── main.js               # Electron main process
├── preload.js            # Context bridge
├── start.sh              # Launch script (port cleanup + backend start)
├── package.json
│
├── renderer/
│   ├── index.html        # UI shell
│   ├── app.js            # WebSocket client, UI logic
│   └── style.css
│
└── backend/
    ├── server.py         # asyncio WebSocket server (entry point)
    ├── camera.py         # OpenCV capture + MediaPipe pose
    ├── calibrator.py     # Neutral-pose calibration
    ├── analyzer.py       # Ollama form analysis + post-session summary
    ├── reporter.py       # 30-day history aggregation + weekly plan
    ├── recorder.py       # Session video recording
    ├── journal.py        # SQLite read/write
    ├── watchbridge.py    # Apple Watch / HealthKit integration
    ├── wakelock.py       # caffeinate wake lock
    ├── voice.py          # Text-to-speech alerts
    ├── sound.py          # Audio cue playback
    ├── logger.py         # Structured logging
    └── pyproject.toml
```

---

## Known limitations

- **Calibration** requires your full torso (shoulders to hips) to be visible. If your camera is mounted on a desk angled upward, step back until your hips are in frame. ([#10](https://github.com/tusharacc/form-check/issues/10))
- **Heart rate** requires `NSHealthShareUsageDescription` in an app bundle `Info.plist`. Running via `npm start` (non-packaged Electron) will log a warning but continue without HR.
- **Inference speed** — `llama3.2-vision:11b` takes ~40 s on M3 with Metal. Form analysis fires every 45 s to stay in sync.
- **Rep counting** works best for exercises with clear elbow or knee angle changes (push-ups, squats, curls, lunges). Static holds (planks) correctly report 0 reps.

---

## License

MIT
