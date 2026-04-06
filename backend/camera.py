"""camera.py — OpenCV capture + MediaPipe Tasks pose estimation.

mediapipe 0.10+ removed the legacy mp.solutions API entirely.
We use the Tasks API (PoseLandmarker with output_segmentation_masks=True)
which provides both skeleton landmarks AND a person-mask in one call.
"""
import pathlib
import urllib.request
import time

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks.python import vision as mpvision
from mediapipe.tasks.python.core import base_options as mpbase

from logger import get_logger

log = get_logger(__name__)

# ── Model auto-download ───────────────────────────────────────────────────────
_MODEL_FILE = pathlib.Path(__file__).parent / "pose_landmarker_full.task"
_MODEL_URL  = (
    "https://storage.googleapis.com/mediapipe-models/"
    "pose_landmarker/pose_landmarker_full/float16/latest/"
    "pose_landmarker_full.task"
)

def _ensure_model() -> str:
    if not _MODEL_FILE.exists():
        log.info("Pose model not found — downloading to %s", _MODEL_FILE)
        urllib.request.urlretrieve(_MODEL_URL, _MODEL_FILE)
        log.info("Pose model download complete (%.1f MB)", _MODEL_FILE.stat().st_size / 1e6)
    else:
        log.debug("Pose model found at %s (%.1f MB)", _MODEL_FILE, _MODEL_FILE.stat().st_size / 1e6)
    return str(_MODEL_FILE)


# ── Drawing helper ────────────────────────────────────────────────────────────
_DRAW    = mpvision.drawing_utils
_CONNS   = mpvision.PoseLandmarksConnections.POSE_LANDMARKS
_LM_SPEC = _DRAW.DrawingSpec(color=(255, 80, 80), thickness=2, circle_radius=4)
_CN_SPEC = _DRAW.DrawingSpec(color=(80, 255, 80), thickness=2)


def draw_skeleton(frame: np.ndarray, landmarks) -> np.ndarray:
    """Draw pose skeleton on a copy of `frame` and return it."""
    out = frame.copy()
    _DRAW.draw_landmarks(
        out, landmarks, _CONNS,
        landmark_drawing_spec=_LM_SPEC,
        connection_drawing_spec=_CN_SPEC,
    )
    return out


# ── Camera class ──────────────────────────────────────────────────────────────
class Camera:
    def __init__(self):
        log.info("Initialising camera (index 0)")
        self.cap = cv2.VideoCapture(0)
        if not self.cap.isOpened():
            log.error("Camera open failed — check Privacy & Security → Camera access")
            raise RuntimeError(
                "Camera not available. Grant camera access to Terminal / Python "
                "in System Settings → Privacy & Security → Camera."
            )

        w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        log.info("Camera opened — resolution=%dx%d  reported_fps=%.1f", w, h, fps)

        # Internal FPS tracking
        self._frame_count = 0
        self._fps_window_start = time.time()
        self._last_fps = 0.0

        model_path = _ensure_model()
        log.debug("Creating PoseLandmarker with Tasks API")
        options = mpvision.PoseLandmarkerOptions(
            base_options=mpbase.BaseOptions(model_asset_path=model_path),
            running_mode=mpvision.RunningMode.IMAGE,
            num_poses=1,
            min_pose_detection_confidence=0.5,
            min_pose_presence_confidence=0.5,
            min_tracking_confidence=0.5,
            output_segmentation_masks=True,   # gives us person mask for free
        )
        self._landmarker = mpvision.PoseLandmarker.create_from_options(options)
        self._last_landmarks = None   # cached from most recent get_annotated_frame call
        log.info("PoseLandmarker ready")

    # ── Public API ────────────────────────────────────────────────────────────

    def get_frame(self) -> np.ndarray | None:
        """Return a raw BGR frame from the webcam, or None on failure."""
        ret, frame = self.cap.read()
        if not ret:
            log.warning("cap.read() returned False — camera may have disconnected")
            return None

        # FPS tracking — log every 300 frames (~10 s at 30 fps)
        self._frame_count += 1
        now = time.time()
        elapsed = now - self._fps_window_start
        if elapsed >= 10.0:
            self._last_fps = self._frame_count / elapsed
            log.debug(
                "Camera FPS: %.1f  (total_frames=%d)",
                self._last_fps, self._frame_count,
            )
            self._fps_window_start = now
            self._frame_count = 0

        return frame

    def get_landmarks(self, frame: np.ndarray):
        """Run pose estimation on `frame` and return landmark list or None."""
        result = self._detect(frame)
        if result and result.pose_landmarks:
            lms = result.pose_landmarks[0]
            # Log visibility of key landmarks at DEBUG level
            visible_count = sum(1 for lm in lms if lm.visibility > 0.6)
            log.debug("Landmarks detected — visible(>0.6): %d/%d", visible_count, len(lms))
            return lms
        log.debug("No pose landmarks detected in frame")
        return None

    def get_annotated_frame(self, proportions=None) -> np.ndarray | None:
        """Capture → segment background → draw skeleton. Returns BGR ndarray."""
        frame = self.get_frame()
        if frame is None:
            return None

        result = self._detect(frame)
        if result is None:
            log.debug("get_annotated_frame: _detect returned None — using raw frame")
            self._last_landmarks = None
            return frame

        self._last_landmarks = result.pose_landmarks[0] if result.pose_landmarks else None

        # ── Background dimming via segmentation mask ──────────────────────────
        if result.segmentation_masks:
            mask = result.segmentation_masks[0].numpy_view()   # float32, shape (H,W) or (H,W,1)
            original_shape = mask.shape
            mask = mask.squeeze()                              # always (H, W)
            if mask.ndim != 2:
                log.warning(
                    "Unexpected mask shape after squeeze: %s (original: %s) — skipping mask",
                    mask.shape, original_shape,
                )
                annotated = frame.copy()
            else:
                bg   = np.full_like(frame, 34)                 # dark background
                annotated = np.where(
                    mask[..., np.newaxis] > 0.5, frame, bg
                ).astype(np.uint8)
                log.debug(
                    "Segmentation mask applied — shape=%s  person_pixels=%.1f%%",
                    mask.shape,
                    100.0 * (mask > 0.5).sum() / mask.size,
                )
        else:
            log.debug("No segmentation mask in result — using raw frame")
            annotated = frame.copy()

        # ── Skeleton overlay ──────────────────────────────────────────────────
        if result.pose_landmarks:
            annotated = draw_skeleton(annotated, result.pose_landmarks[0])
        else:
            log.debug("No pose landmarks for skeleton overlay")

        return annotated

    def close(self) -> None:
        log.info(
            "Closing camera — total_frames_captured=%d  last_fps=%.1f",
            self._frame_count, self._last_fps,
        )
        self.cap.release()
        self._landmarker.close()
        log.debug("Camera and landmarker released")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _detect(self, frame: np.ndarray):
        """Convert frame to mediapipe Image and run PoseLandmarker."""
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        try:
            return self._landmarker.detect(mp_image)
        except Exception as exc:
            log.error(
                "_detect exception: %s — frame.shape=%s  frame.dtype=%s",
                exc, frame.shape, frame.dtype,
            )
            return None
