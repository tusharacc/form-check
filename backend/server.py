"""server.py — asyncio WebSocket server for FormCheck.

Orchestrates four concurrent asyncio tasks per client:
  camera_loop       — streams annotated JPEG frames at ~30 fps
  analysis_loop     — runs Ollama form analysis every ANALYSIS_INTERVAL seconds
  heart_rate_loop   — polls Apple Watch heart rate every 30 s (best-effort)
  handle_client     — processes control messages (start/stop session)

ANALYSIS_INTERVAL is set to 45 s to match the real M3 Metal GPU inference
cadence (~40 s inference + overhead). The old 5 s value was misleading.
"""
import asyncio
import base64
import json
import logging
import math
import os
import time
from pathlib import Path

import cv2
import websockets

from analyzer    import Analyzer, FormCheckResult
from calibrator  import Calibrator, CalibrationError
from camera      import Camera
from journal     import init_db, start_session, log_event, end_session, update_session_video
from logger      import get_logger, log_path
from recorder    import SessionRecorder
from voice       import VoiceEngine
from sound       import SoundEngine
from watchbridge import WatchBridge
from wakelock    import WakeLock

log  = get_logger(__name__)
slog = get_logger("session")
alog = get_logger("analysis")
clog = get_logger("camera")

# Suppress "opening handshake failed" noise from bare-TCP port probes
# (main.js uses net.createConnection to wait for the port to be ready)
logging.getLogger("websockets.server").setLevel(logging.ERROR)

ANALYSIS_INTERVAL = 45   # seconds — matches real ~40 s M3 Metal inference latency
HR_INTERVAL       = 30   # seconds between heart rate polls
WS_HOST           = "0.0.0.0"
WS_PORT           = 8765
DB_PATH           = str(Path(__file__).parent / "formcheck.db")


# ── Module-level helpers ──────────────────────────────────────────────────────

def _normalise_exercise(name: str | None) -> str | None:
    """Strip, collapse whitespace, title-case for consistent display."""
    if not name:
        return name
    return " ".join(name.strip().split()).title()


def _joint_angle(lm_list, a: int, b: int, c: int) -> float:
    """Angle in degrees at joint b, given landmarks a-b-c."""
    A  = (lm_list[a].x, lm_list[a].y)
    B  = (lm_list[b].x, lm_list[b].y)
    C  = (lm_list[c].x, lm_list[c].y)
    BA = (A[0] - B[0], A[1] - B[1])
    BC = (C[0] - B[0], C[1] - B[1])
    dot    = BA[0] * BC[0] + BA[1] * BC[1]
    mag_ba = math.sqrt(BA[0] ** 2 + BA[1] ** 2)
    mag_bc = math.sqrt(BC[0] ** 2 + BC[1] ** 2)
    if mag_ba == 0 or mag_bc == 0:
        return 180.0
    cosine = max(-1.0, min(1.0, dot / (mag_ba * mag_bc)))
    return math.degrees(math.acos(cosine))


# Named joints sampled for rep counting (MediaPipe landmark indices)
_JOINTS = {
    "left_elbow":  (11, 13, 15),
    "right_elbow": (12, 14, 16),
    "left_knee":   (23, 25, 27),
    "right_knee":  (24, 26, 28),
}


def _count_reps_series(series: list) -> int:
    """Count rep cycles from a single joint's angle time series.

    A rep = angle crosses below LOW then above HIGH (or vice versa).
    Returns complete cycles (transitions // 2).
    """
    LOW, HIGH = 110.0, 150.0
    if not series:
        return 0
    state       = None
    transitions = 0
    for angle in series:
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


def _count_reps(angle_dict: dict) -> int:
    """Count reps across all tracked joints; return the max over all joints.

    Each joint's series is counted independently so interleaving different
    joints' angles (which have unrelated magnitudes) never creates false
    transitions.  The joint with the most reps wins — that's the one actually
    driving the exercise motion.
    """
    counts = [_count_reps_series(s) for s in angle_dict.values() if s]
    return max(counts) if counts else 0


def _sample_keyframes(video_path, interval_s: int = 30, max_frames: int = 5) -> list:
    """Sample up to max_frames frames from video_path, one every interval_s seconds."""
    import cv2 as _cv2
    cap = _cv2.VideoCapture(str(video_path))
    try:
        fps   = cap.get(_cv2.CAP_PROP_FPS)
        if fps <= 0:
            fps = 15.0   # fallback: known recording FPS from SessionRecorder
            log.debug("_sample_keyframes: CAP_PROP_FPS=0, using fallback fps=%.1f", fps)
        total = int(cap.get(_cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0:
            log.warning("_sample_keyframes: invalid video (fps=%.1f total=%d) — %s", fps, total, video_path)
            return []
        indices = [
            int(i * interval_s * fps)
            for i in range(max_frames)
            if int(i * interval_s * fps) < total
        ]
        frames = []
        for idx in indices:
            cap.set(_cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if ret:
                frames.append(frame)
        log.debug("_sample_keyframes: sampled %d/%d frames from %s", len(frames), len(indices), video_path)
        return frames
    finally:
        cap.release()


# ── Camera loop ───────────────────────────────────────────────────────────────

async def camera_loop(websocket, camera: Camera, state: dict) -> None:
    """Stream annotated JPEG frames to the client at ~30 fps.

    Also writes every other frame to the session recorder when a session is active,
    and samples joint angles every 3 frames (~10 Hz) for rep counting.
    """
    clog.info("camera_loop starting")
    _frame_counter = 0
    _fps_count     = 0
    _fps_start     = time.time()

    while True:
        frame = camera.get_annotated_frame(state.get("proportions"))
        if frame is None:
            clog.warning("camera_loop: get_annotated_frame returned None — skipping frame")
            await asyncio.sleep(0.033)
            continue

        _frame_counter += 1
        _fps_count     += 1

        # FPS logging every 5 s
        elapsed = time.time() - _fps_start
        if elapsed >= 5.0:
            fps = _fps_count / elapsed
            clog.debug("camera_loop FPS: %.1f  (total_frames=%d)", fps, _frame_counter)
            _fps_count = 0
            _fps_start = time.time()

        # Encode and send to client
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            clog.warning("camera_loop: imencode failed")
            await asyncio.sleep(0.033)
            continue

        b64 = base64.b64encode(buf.tobytes()).decode("utf-8")
        try:
            await websocket.send(json.dumps({"type": "frame", "data": b64}))
        except websockets.exceptions.ConnectionClosed:
            clog.info("camera_loop: connection closed — exiting")
            return

        # Write every other frame to recorder when session is active
        if _frame_counter % 2 == 0 and state.get("active") and state.get("recorder"):
            state["recorder"].write_frame(frame)

        # Sample joint angles every 3 frames (~10 Hz) for rep counting
        if _frame_counter % 3 == 0 and state.get("active"):
            lms = camera._last_landmarks
            if lms is not None:
                for name, (a, b, c) in _JOINTS.items():
                    if max(a, b, c) < len(lms):
                        state["angle_series"][name].append(_joint_angle(lms, a, b, c))

        await asyncio.sleep(0.033)  # ~30 fps target


# ── Analysis loop ─────────────────────────────────────────────────────────────

async def analysis_loop(websocket, camera: Camera, analyzer: Analyzer,
                        voice: VoiceEngine, sound: SoundEngine, state: dict) -> None:
    """Run form analysis every ANALYSIS_INTERVAL seconds while a session is active.

    Calls analyze_form() in an executor thread so inference (~40 s) doesn't block
    the asyncio event loop. Tracks joint angles for post-session rep counting.
    """
    alog.info("analysis_loop starting (interval=%ds)", ANALYSIS_INTERVAL)
    loop = asyncio.get_running_loop()

    while True:
        await asyncio.sleep(ANALYSIS_INTERVAL)

        if not state.get("active"):
            alog.debug("analysis_loop: session not active — skipping analysis")
            continue

        raw_frame = camera.get_frame()
        if raw_frame is None:
            alog.warning("analysis_loop: get_frame returned None — skipping")
            continue

        # Landmark angle tracking for post-session rep counting
        try:
            landmarks = camera.get_landmarks(raw_frame)
            if landmarks is not None:
                # Track elbow and knee angles from both sides
                for (a, b, c) in [(11, 13, 15), (12, 14, 16), (23, 25, 27), (24, 26, 28)]:
                    if max(a, b, c) < len(landmarks):
                        state["angle_series"].append(_joint_angle(landmarks, a, b, c))
        except Exception as _lm_err:
            alog.debug("Landmark angle tracking failed: %s", _lm_err)

        alog.debug("analysis_loop: dispatching analyze_form to executor")
        t_start = time.time()

        try:
            result: FormCheckResult = await loop.run_in_executor(
                None, analyzer.analyze_form, raw_frame
            )
        except Exception as exc:
            alog.error("analysis_loop: analyze_form raised: %s", exc)
            continue

        # Guard: session may have ended while inference ran
        if not state.get("active"):
            alog.debug("analysis_loop: session ended during inference — discarding result")
            continue

        latency = round(time.time() - t_start, 1)
        sid     = state.get("session_id")

        # Collect exercise name identified in this real-time frame
        ex_name = result.exercise
        if ex_name:
            exs = state["detected_exercises"]
            if ex_name not in exs:
                exs.append(ex_name)
                alog.info("Exercise identified: %s (total distinct: %d)", ex_name, len(exs))

        alog.info(
            "Analysis — exercise=%s severity=%s  issues=%s  latency=%.1fs  session_id=%s",
            ex_name or "?",
            result.severity,
            result.issues if result.issues else "none",
            latency, sid,
        )

        # Log to journal DB
        if sid is not None:
            for issue in (result.issues or [""]):
                log_event(
                    session_id=sid,
                    exercise=ex_name or None,
                    severity=result.severity,
                    issue=issue or "",
                    tip=result.tip,
                )

        # Trigger voice / sound alerts
        if result.severity in ("WARNING", "CRITICAL") and result.tip:
            voice.speak(result.tip)
            sound.alert(result.severity)

        # Send analysis message to client (no exercise field — that's post-session)
        try:
            await websocket.send(json.dumps({
                "type":      "analysis",
                "severity":  result.severity,
                "issues":    result.issues,
                "tip":       result.tip,
                "latency_s": latency,
            }))
        except websockets.exceptions.ConnectionClosed:
            alog.info("analysis_loop: connection closed — exiting")
            return


# ── Heart rate loop ───────────────────────────────────────────────────────────

async def heart_rate_loop(websocket, watch: WatchBridge, state: dict) -> None:
    """Poll Apple Watch heart rate every HR_INTERVAL seconds during an active session.

    Sends {"type": "heart_rate", "bpm": <int>} when a reading is available.
    Silently skips when HealthKit is unavailable or returns None.
    """
    loop = asyncio.get_running_loop()
    log.info("heart_rate_loop starting (interval=%ds)", HR_INTERVAL)

    while True:
        await asyncio.sleep(HR_INTERVAL)

        if not state.get("active"):
            continue

        try:
            bpm = await loop.run_in_executor(None, watch.get_heart_rate)
        except Exception as exc:
            log.warning("heart_rate_loop: get_heart_rate raised: %s", exc)
            continue

        if bpm is None:
            continue

        try:
            await websocket.send(json.dumps({"type": "heart_rate", "bpm": bpm}))
            log.debug("heart_rate_loop: sent bpm=%d", bpm)
        except websockets.exceptions.ConnectionClosed:
            log.info("heart_rate_loop: connection closed — exiting")
            return


# ── Client handler ────────────────────────────────────────────────────────────

async def handle_client(websocket) -> None:
    """Main handler for one WebSocket connection.

    Lifecycle:
      1. Send "calibrating" message
      2. Start camera_loop so the client sees live video during calibration
      3. Run Calibrator in a thread executor (async-safe, non-blocking)
      4. Start analysis_loop + heart_rate_loop
      5. Process start_session / stop_session messages
    """
    log.info("Client connected — addr=%s", websocket.remote_address)

    camera   = Camera()
    analyzer = Analyzer()
    voice    = VoiceEngine()
    sound    = SoundEngine()
    watch    = WatchBridge()
    wakelock = WakeLock()

    state = {
        "proportions":        None,
        "active":             False,
        "session_id":         None,
        "recorder":           None,
        "angle_series":       {k: [] for k in _JOINTS},   # per-joint, never interleaved
        "detected_exercises": [],                          # names from real-time analysis
    }

    # Start camera stream immediately so the user sees themselves during calibration
    cam_task = asyncio.create_task(camera_loop(websocket, camera, state))

    try:
        # ── Calibration ───────────────────────────────────────────────────────
        await websocket.send(json.dumps({"type": "calibrating"}))
        log.info("Calibration phase starting")

        loop = asyncio.get_running_loop()

        # Request HealthKit authorization — fire-and-forget, never block calibration
        loop.run_in_executor(None, watch.request_authorization)

        calibrator = Calibrator()
        try:
            proportions = await calibrator.run_async(camera)
            state["proportions"] = proportions
            await websocket.send(json.dumps({"type": "calibration_done"}))
            log.info("Calibration succeeded — proportions stored in state")
        except CalibrationError as cal_err:
            await websocket.send(json.dumps({
                "type":   "calibration_failed",
                "reason": str(cal_err),
            }))
            log.warning("Calibration failed: %s — continuing with proportions=None", cal_err)
            # Continue without calibration; skeleton draws but no deviation indicators

        # ── Start analysis + heart rate loops ─────────────────────────────────
        ana_task = asyncio.create_task(
            analysis_loop(websocket, camera, analyzer, voice, sound, state)
        )
        hr_task = asyncio.create_task(heart_rate_loop(websocket, watch, state))

        # ── Message loop ──────────────────────────────────────────────────────
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Received non-JSON message — ignored")
                continue

            msg_type = msg.get("type")
            log.debug("Received message: type=%s", msg_type)

            if msg_type == "start_session":
                if state["active"]:
                    log.warning("start_session received but session already active — ignored")
                    continue

                sid = start_session()
                state["session_id"]         = sid
                state["active"]             = True
                state["angle_series"]       = {k: [] for k in _JOINTS}
                state["detected_exercises"] = []

                # Start video recording
                try:
                    w = int(camera.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    h = int(camera.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    state["recorder"] = SessionRecorder(width=w, height=h, fps=15)
                    state["recorder"].start()
                    slog.info("Recording started for session_id=%d", sid)
                except Exception as rec_err:
                    slog.warning("Failed to start recorder: %s — continuing without recording", rec_err)
                    state["recorder"] = None

                # Prevent laptop sleep
                wakelock.acquire()

                # Notify Apple Watch
                await loop.run_in_executor(None, watch.notify_session_start)

                slog.info("Session started — session_id=%d", sid)
                await websocket.send(json.dumps({
                    "type":       "session_started",
                    "session_id": sid,
                }))

            elif msg_type == "stop_session":
                if not state["active"]:
                    log.warning("stop_session received but no session active — ignored")
                    continue

                sid = state["session_id"]
                state["active"]     = False
                state["session_id"] = None
                slog.info("Session stopping — session_id=%d", sid)

                # Release wake lock and notify Watch
                wakelock.release()
                await loop.run_in_executor(None, watch.notify_session_stop)

                # Step 1 — stop recording
                recorder   = state.get("recorder")
                video_path = recorder.stop() if recorder is not None else None
                state["recorder"] = None
                slog.info("Recording stopped — video_path=%s", video_path)

                # Step 2 — update DB
                if sid is not None:
                    end_session(sid, summary="Session ended by user")
                if video_path is not None and sid is not None:
                    update_session_video(sid, str(video_path))

                # Step 3 — sample keyframes
                keyframes = []
                if video_path is not None:
                    try:
                        keyframes = _sample_keyframes(video_path)
                        slog.info("Sampled %d keyframes from %s", len(keyframes), video_path)
                    except Exception as _kf_err:
                        slog.warning("Keyframe sampling failed: %s", _kf_err)

                # Step 4 — exercise names: prefer real-time identifications collected
                # during analysis_loop; fall back to post-session Ollama keyframe scan
                # only if none were found during the session.
                realtime_exercises = [
                    _normalise_exercise(n) for n in state["detected_exercises"] if n
                ]

                summary_result = None
                if not realtime_exercises and keyframes:
                    # No real-time names — try post-session keyframe scan as fallback
                    try:
                        summary_result = await loop.run_in_executor(
                            None, analyzer.analyze_summary, keyframes
                        )
                        slog.info(
                            "Post-session summary fallback: exercises=%s",
                            [e.get("name") for e in summary_result.exercises],
                        )
                    except Exception as _sum_err:
                        slog.warning(
                            "analyze_summary fallback failed: %s", _sum_err
                        )

                # Step 5 — count angle-based reps (per-joint, take max)
                angle_reps = _count_reps(state["angle_series"])
                total_samples = sum(len(s) for s in state["angle_series"].values())
                slog.info(
                    "Angle-based rep count: %d  (samples per joint: %s)",
                    angle_reps,
                    {k: len(v) for k, v in state["angle_series"].items()},
                )

                # Step 6 — build final exercise list
                exercises = realtime_exercises or []
                if not exercises and summary_result is not None:
                    seen = set()
                    for e in summary_result.exercises:
                        name = _normalise_exercise(e.get("name", ""))
                        if name and name.lower() not in ("unknown", "n/a") and name not in seen:
                            exercises.append(name)
                            seen.add(name)

                await websocket.send(json.dumps({
                    "type":       "session_summary",
                    "exercises":  exercises,
                    "total_reps": angle_reps,
                }))
                slog.info(
                    "session_summary sent — exercises=%s total_reps=%d",
                    exercises, angle_reps,
                )

                # Step 7 — reset state for next session
                state["angle_series"]       = {k: [] for k in _JOINTS}
                state["detected_exercises"] = []

            elif msg_type == "set_interval":
                # Future: allow UI to change analysis interval
                new_interval = msg.get("seconds")
                log.info("set_interval received: %s (not applied at runtime)", new_interval)

            else:
                log.debug("Unknown message type ignored: %s", msg_type)

    except websockets.exceptions.ConnectionClosed as exc:
        log.info("WebSocket connection closed — code=%s reason=%s", exc.code, exc.reason)
    except Exception as exc:
        log.error("Unhandled exception in handle_client: %s", exc, exc_info=True)
    finally:
        log.info("Cleaning up — cancelling tasks, closing camera")
        wakelock.release()  # always release, even on unexpected disconnect
        cam_task.cancel()
        try:
            ana_task.cancel()
        except UnboundLocalError:
            pass
        try:
            hr_task.cancel()
        except UnboundLocalError:
            pass
        camera.close()

        # Ensure any in-progress recording is stopped cleanly
        if state.get("recorder") is not None:
            try:
                state["recorder"].stop()
            except Exception:
                pass

        log.info("Client handler complete")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    log.info(
        "FormCheck WebSocket server starting — ws://%s:%d",
        WS_HOST, WS_PORT,
    )
    log.info("Log file: %s", log_path())
    log.info("DB path:  %s", DB_PATH)

    init_db(DB_PATH)
    log.info("Database initialised")

    async with websockets.serve(handle_client, WS_HOST, WS_PORT):
        log.info("Server ready — waiting for connections")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
