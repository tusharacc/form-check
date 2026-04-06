"""calibrator.py — Neutral-pose calibration for FormCheck.

Captures ~150 webcam frames while the user holds a still, upright stance.
Filters for frames with high-confidence upper-body landmarks, then averages
them to produce BodyProportions — normalised body ratios used by camera.py
to render deviation indicators during a session.
"""
import asyncio
import math
import time
from dataclasses import dataclass

from logger import get_logger

log = get_logger(__name__)

# Landmark indices used for calibration validation (upper-body subset)
# MediaPipe Pose: 11=L_shoulder 12=R_shoulder 13=L_elbow 14=R_elbow
#                 15=L_wrist   16=R_wrist   23=L_hip   24=R_hip
_UPPER_BODY_LANDMARKS = (11, 12, 13, 14, 15, 16, 23, 24)

_MIN_VISIBILITY     = 0.6    # landmark must exceed this to count as visible
_FRAMES_NEEDED      = 150    # total frames to attempt
_MIN_VALID_FRAMES   = 30     # minimum usable frames; raises CalibrationError if below
_YIELD_INTERVAL     = 0.05   # seconds between yields in run_async


class CalibrationError(RuntimeError):
    """Raised when not enough high-quality frames were captured."""


@dataclass
class BodyProportions:
    """Body measurements as fractions of frame height (scale-invariant).

    All values are normalised to [0, 1] relative to the frame height so that
    a user sitting closer/farther from the camera doesn't shift the thresholds.
    """
    shoulder_width:   float   # horizontal distance between shoulders
    torso_height:     float   # midpoint(shoulders) → midpoint(hips), vertical
    left_arm_len:     float   # shoulder → elbow + elbow → wrist (left side)
    right_arm_len:    float   # shoulder → elbow + elbow → wrist (right side)
    left_leg_len:     float   # hip → knee + knee → ankle, estimated from hip (left)
    right_leg_len:    float   # hip → knee + knee → ankle, estimated from hip (right)


def _dist(a, b) -> float:
    """Euclidean distance between two landmarks (using normalised x, y only)."""
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2)


def _midpoint_y(a, b) -> float:
    return (a.y + b.y) / 2.0


def _valid_frame(landmarks) -> bool:
    """Return True if all upper-body landmarks exceed the visibility threshold."""
    for idx in _UPPER_BODY_LANDMARKS:
        if idx >= len(landmarks):
            return False
        if landmarks[idx].visibility < _MIN_VISIBILITY:
            return False
    return True


def _extract_proportions(landmarks) -> BodyProportions:
    """Compute proportions from a single frame's landmarks."""
    lm = landmarks
    shoulder_width = _dist(lm[11], lm[12])

    torso_top_y    = _midpoint_y(lm[11], lm[12])
    torso_bot_y    = _midpoint_y(lm[23], lm[24])
    torso_height   = abs(torso_bot_y - torso_top_y)

    left_arm_len   = _dist(lm[11], lm[13]) + _dist(lm[13], lm[15])
    right_arm_len  = _dist(lm[12], lm[14]) + _dist(lm[14], lm[16])

    # Legs: use hip-to-knee distance scaled by torso ratio (ankles often occluded)
    left_leg_len   = _dist(lm[23], lm[25]) if len(lm) > 27 else torso_height * 1.4
    right_leg_len  = _dist(lm[24], lm[26]) if len(lm) > 28 else torso_height * 1.4

    return BodyProportions(
        shoulder_width = shoulder_width,
        torso_height   = torso_height,
        left_arm_len   = left_arm_len,
        right_arm_len  = right_arm_len,
        left_leg_len   = left_leg_len,
        right_leg_len  = right_leg_len,
    )


def _average_proportions(samples: list) -> BodyProportions:
    n = len(samples)
    return BodyProportions(
        shoulder_width = sum(s.shoulder_width for s in samples) / n,
        torso_height   = sum(s.torso_height   for s in samples) / n,
        left_arm_len   = sum(s.left_arm_len   for s in samples) / n,
        right_arm_len  = sum(s.right_arm_len  for s in samples) / n,
        left_leg_len   = sum(s.left_leg_len   for s in samples) / n,
        right_leg_len  = sum(s.right_leg_len  for s in samples) / n,
    )


class Calibrator:
    """Captures and analyses calibration frames to establish body proportions.

    Two entry points:
    - run(camera)       — blocking (call from a thread executor)
    - run_async(camera) — async coroutine that yields every ~50 ms so the
                          camera_loop can continue streaming frames to the client
    """

    # ── Blocking variant (for run_in_executor) ────────────────────────────────

    def run(self, camera) -> BodyProportions:
        """Collect up to _FRAMES_NEEDED frames, return averaged BodyProportions.

        Raises CalibrationError if fewer than _MIN_VALID_FRAMES are usable.
        This method is blocking — always call via run_in_executor in async code.
        """
        log.info("Calibration starting — collecting %d frames", _FRAMES_NEEDED)
        t_start   = time.time()
        valid     = []
        rejected  = 0

        for frame_idx in range(_FRAMES_NEEDED):
            frame = camera.get_frame()
            if frame is None:
                rejected += 1
                if frame_idx % 10 == 0:
                    log.debug(
                        "Calibration frame %d/%d — camera returned None (rejected=%d)",
                        frame_idx + 1, _FRAMES_NEEDED, rejected,
                    )
                continue

            landmarks = camera.get_landmarks(frame)
            if landmarks is None or not _valid_frame(landmarks):
                rejected += 1
                if frame_idx % 10 == 0:
                    log.debug(
                        "Calibration frame %d/%d — landmarks missing/low-conf (valid=%d rejected=%d)",
                        frame_idx + 1, _FRAMES_NEEDED, len(valid), rejected,
                    )
                continue

            props = _extract_proportions(landmarks)
            valid.append(props)

            if frame_idx % 10 == 0:
                log.debug(
                    "Calibration frame %d/%d — valid=%d rejected=%d "
                    "shoulder_w=%.3f torso_h=%.3f",
                    frame_idx + 1, _FRAMES_NEEDED, len(valid), rejected,
                    props.shoulder_width, props.torso_height,
                )

        elapsed = time.time() - t_start
        log.info(
            "Calibration capture complete — %.1fs  valid=%d  rejected=%d",
            elapsed, len(valid), rejected,
        )

        if len(valid) < _MIN_VALID_FRAMES:
            log.warning(
                "Calibration failed — only %d valid frames (need %d). "
                "Ensure your full upper body is visible and well-lit.",
                len(valid), _MIN_VALID_FRAMES,
            )
            raise CalibrationError(
                f"Only {len(valid)} usable frames captured "
                f"(need ≥ {_MIN_VALID_FRAMES}). "
                "Stand with your full upper body in frame and try again."
            )

        result = _average_proportions(valid)
        log.info(
            "Calibration succeeded — proportions: "
            "shoulder_w=%.3f torso_h=%.3f "
            "l_arm=%.3f r_arm=%.3f l_leg=%.3f r_leg=%.3f",
            result.shoulder_width, result.torso_height,
            result.left_arm_len,   result.right_arm_len,
            result.left_leg_len,   result.right_leg_len,
        )
        return result

    # ── Async variant (yields control to the event loop) ─────────────────────

    async def run_async(self, camera) -> BodyProportions:
        """Async wrapper around run() that yields every _YIELD_INTERVAL seconds.

        Captures frames in small bursts so the asyncio event loop (camera_loop,
        WebSocket I/O) keeps running during calibration — ensuring the client
        continues to receive live frames while calibration is in progress.
        """
        log.info(
            "Calibration run_async starting — collecting %d frames with %.2fs yields",
            _FRAMES_NEEDED, _YIELD_INTERVAL,
        )
        t_start   = time.time()
        valid     = []
        rejected  = 0

        for frame_idx in range(_FRAMES_NEEDED):
            frame = camera.get_frame()
            if frame is None:
                rejected += 1
            else:
                landmarks = camera.get_landmarks(frame)
                if landmarks is None or not _valid_frame(landmarks):
                    rejected += 1
                else:
                    valid.append(_extract_proportions(landmarks))

            if frame_idx % 10 == 0:
                log.debug(
                    "Calibration async frame %d/%d — valid=%d rejected=%d",
                    frame_idx + 1, _FRAMES_NEEDED, len(valid), rejected,
                )

            # Yield to event loop periodically so camera_loop stays alive
            if frame_idx % 3 == 0:
                await asyncio.sleep(_YIELD_INTERVAL)

        elapsed = time.time() - t_start
        log.info(
            "Calibration run_async complete — %.1fs  valid=%d  rejected=%d",
            elapsed, len(valid), rejected,
        )

        if len(valid) < _MIN_VALID_FRAMES:
            log.warning(
                "Calibration run_async failed — %d valid frames (need %d)",
                len(valid), _MIN_VALID_FRAMES,
            )
            raise CalibrationError(
                f"Only {len(valid)} usable frames captured "
                f"(need ≥ {_MIN_VALID_FRAMES}). "
                "Stand with your full upper body in frame and try again."
            )

        result = _average_proportions(valid)
        log.info(
            "Calibration run_async succeeded — "
            "shoulder_w=%.3f torso_h=%.3f l_arm=%.3f r_arm=%.3f",
            result.shoulder_width, result.torso_height,
            result.left_arm_len, result.right_arm_len,
        )
        return result
