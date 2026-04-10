"""reanalyzer.py — Dense video reanalysis for stored session recordings.

Samples frames every SAMPLE_INTERVAL seconds, sends clips of CLIP_SIZE
consecutive frames to formcheck-vision (or llama3.2-vision:11b fallback)
and writes identified exercises back to the events table.

Public API
----------
reanalyze_session(session_id, video_path, progress_cb=None) -> dict
    Blocking — always call via run_in_executor.
    Returns {"exercises": [...], "clips_analyzed": int, "duration_s": int}
"""
import base64
import json
import sqlite3
import time
from pathlib import Path

import cv2
import ollama

from logger import get_logger

log = get_logger(__name__)

_DB_PATH        = str(Path(__file__).parent / "formcheck.db")
_SAMPLE_INTERVAL = 5     # seconds between clip start points
_CLIP_SIZE       = 3     # frames per clip (shows motion context)
_CLIP_STRIDE     = 10    # frames between each frame in a clip (at 15fps ≈ 0.67s gap)
_PREFERRED_MODEL = "formcheck-vision"
_FALLBACK_MODEL  = "llama3.2-vision:11b"

_PROMPT = (
    "These frames are from a workout session. "
    "Identify the exercise being performed. "
    "If the exercise changes between frames, identify the most prominent one. "
    "Reply with JSON only."
)


def _model_available(name: str) -> bool:
    try:
        models = ollama.list()
        return any(m.model.startswith(name) for m in models.models)
    except Exception:
        return False


def _encode(frame) -> str:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
    if not ok:
        raise RuntimeError("imencode failed")
    return base64.b64encode(buf.tobytes()).decode()


def _sample_clips(video_path: str) -> list[list]:
    """Sample clips from video. Each clip = list of frames (numpy arrays)."""
    cap = cv2.VideoCapture(video_path)
    try:
        fps   = cap.get(cv2.CAP_PROP_FPS) or 15.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        dur_s = total / fps

        step  = int(_SAMPLE_INTERVAL * fps)   # frames between clip start points
        clips = []

        start_idx = 0
        while start_idx < total:
            frames = []
            for i in range(_CLIP_SIZE):
                idx = start_idx + i * _CLIP_STRIDE
                if idx >= total:
                    break
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ret, frame = cap.read()
                if ret:
                    frames.append(frame)
            if frames:
                clips.append((start_idx / fps, frames))   # (timestamp_s, frames)
            start_idx += step

        log.info(
            "reanalyzer: sampled %d clips from %.0fs video (%s)",
            len(clips), dur_s, Path(video_path).name,
        )
        return clips, dur_s
    finally:
        cap.release()


def _analyze_clip(model: str, frames: list) -> dict:
    """Send a clip to Ollama; return parsed dict or empty dict on failure."""
    images = []
    for f in frames:
        try:
            images.append(_encode(f))
        except Exception:
            pass
    if not images:
        return {}

    try:
        resp = ollama.chat(
            model=model,
            messages=[{"role": "user", "content": _PROMPT, "images": images}],
            options={"temperature": 0.0},
        )
        raw = resp.message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("reanalyzer: JSON parse failed for clip")
        return {}
    except Exception as exc:
        log.warning("reanalyzer: Ollama error: %s", exc)
        return {}


def _write_events(session_id: int, events: list[dict]) -> None:
    """Insert reanalysis events into the events table, tagged with source."""
    conn = sqlite3.connect(_DB_PATH)
    c    = conn.cursor()
    for ev in events:
        c.execute(
            "INSERT INTO events (session_id, ts, exercise, severity, issue, tip) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                session_id,
                ev["ts"],
                ev["exercise"],
                ev.get("severity", "OK"),
                ev.get("issue", "reanalysis"),
                ev.get("tip", ""),
            ],
        )
    conn.commit()
    conn.close()
    log.info("reanalyzer: wrote %d events for session_id=%d", len(events), session_id)


def _normalise(name: str) -> str:
    return " ".join(name.strip().split()).title() if name else ""


def reanalyze_session(
    session_id: int,
    video_path: str,
    progress_cb=None,       # optional callable(pct: int, msg: str)
) -> dict:
    """Dense reanalysis of a stored session video.

    Blocking — call via loop.run_in_executor.
    Returns {"exercises": [{"name", "count", "timestamps"}], "clips_analyzed": int, "duration_s": int}
    """
    log.info("reanalyzer: starting session_id=%d  video=%s", session_id, video_path)

    model = _PREFERRED_MODEL if _model_available(_PREFERRED_MODEL) else _FALLBACK_MODEL
    log.info("reanalyzer: using model=%s", model)

    if progress_cb:
        progress_cb(0, f"Sampling video frames…")

    clips, dur_s = _sample_clips(video_path)
    if not clips:
        log.warning("reanalyzer: no clips extracted from %s", video_path)
        return {"exercises": [], "clips_analyzed": 0, "duration_s": int(dur_s)}

    events       = []
    ex_timeline  = {}   # name → list of timestamps

    for i, (ts, frames) in enumerate(clips):
        pct = int((i / len(clips)) * 100)
        if progress_cb:
            progress_cb(pct, f"Analyzing clip {i+1}/{len(clips)}…")

        result = _analyze_clip(model, frames)
        raw_name = result.get("exercise", "")
        if not raw_name or raw_name.lower() in ("unknown", "n/a", ""):
            continue

        name = _normalise(raw_name)
        if name not in ex_timeline:
            ex_timeline[name] = []
        ex_timeline[name].append(round(ts))

        from datetime import datetime, timezone
        event_ts = datetime.now(timezone.utc).isoformat()
        events.append({
            "ts":       event_ts,
            "exercise": name,
            "severity": result.get("severity", "OK"),
            "issue":    result.get("notes", "reanalysis"),
            "tip":      result.get("tip", ""),
        })

        log.debug(
            "reanalyzer: clip %d/%d  t=%.0fs  exercise=%s  confidence=%s",
            i + 1, len(clips), ts, name, result.get("confidence", "?"),
        )

    # Persist to DB
    if events:
        _write_events(session_id, events)

    if progress_cb:
        progress_cb(100, "Done")

    # Build summary
    exercises = [
        {
            "name":       name,
            "count":      len(timestamps),
            "timestamps": timestamps,
        }
        for name, timestamps in sorted(ex_timeline.items(), key=lambda x: -len(x[1]))
    ]

    log.info(
        "reanalyzer: session_id=%d — %d distinct exercises found in %d clips",
        session_id, len(exercises), len(clips),
    )
    return {
        "exercises":      exercises,
        "clips_analyzed": len(clips),
        "duration_s":     int(dur_s),
    }
