"""recorder.py — Session video recording for FormCheck.

Writes annotated frames to an MP4 file during each session.
Enforces a maximum of MAX_RECORDINGS files; oldest are deleted automatically.

Frame-rate throttling
---------------------
MediaPipe runs at ~4–5 fps on the main loop, but the VideoWriter is declared
at TARGET_FPS (15).  Without throttling, cv2.VideoWriter stamps every frame
as 1/15 s apart even though real time between frames is ~200 ms — the video
plays 3× faster than real life.

write_frame() uses a wall-clock gate: it only accepts a frame if at least
1/TARGET_FPS seconds have elapsed since the last accepted frame.  This keeps
the video's frame count ≈ TARGET_FPS × real_duration_s so playback speed
matches real time.
"""
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from logger import get_logger

log = get_logger(__name__)

RECORDINGS_DIR  = Path(__file__).parent / "recordings"
MAX_RECORDINGS  = 7


class SessionRecorder:
    """Records annotated webcam frames to MP4 during a FormCheck session.

    Usage:
        recorder = SessionRecorder(width=1280, height=720, fps=15)
        path = recorder.start()          # returns Path to the mp4 file
        recorder.write_frame(frame)      # call each frame (throttled by wall clock)
        final_path = recorder.stop()     # releases writer, enforces 7-file limit
    """

    def __init__(self, width: int, height: int, fps: int = 15) -> None:
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        self.width  = width
        self.height = height
        self.fps    = fps
        self._frame_interval = 1.0 / fps   # minimum wall-clock gap between writes

        self._writer: cv2.VideoWriter | None = None
        self._video_path: Path | None        = None
        self._last_write_time: float         = 0.0
        self._frames_written: int            = 0
        self._frames_skipped: int            = 0

        log.info(
            "SessionRecorder initialised — size=%dx%d fps=%d recordings_dir=%s",
            self.width, self.height, self.fps, RECORDINGS_DIR,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> Path:
        """Open a new VideoWriter and return the file path.

        Raises RuntimeError if a recording is already active.
        """
        if self._writer is not None:
            raise RuntimeError("Recording already active — call stop() first")

        filename = f"session_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.mp4"
        path     = RECORDINGS_DIR / filename
        fourcc   = cv2.VideoWriter_fourcc(*"mp4v")

        self._writer          = cv2.VideoWriter(str(path), fourcc, self.fps, (self.width, self.height))
        self._video_path      = path
        self._last_write_time = 0.0   # accept the very first frame immediately
        self._frames_written  = 0
        self._frames_skipped  = 0

        log.info("Recording started — path=%s", path)
        return path

    def write_frame(self, frame: np.ndarray) -> None:
        """Write a frame only if enough wall-clock time has passed since the last write.

        This throttles the write rate to self.fps regardless of how fast
        the caller delivers frames, ensuring the video plays back at real speed.
        """
        if self._writer is None:
            return
        now = time.monotonic()
        if now - self._last_write_time < self._frame_interval:
            self._frames_skipped += 1
            return
        self._writer.write(frame)
        self._last_write_time = now
        self._frames_written  += 1

    def stop(self) -> Path | None:
        """Finalise the recording and return the file path.

        Returns None if no recording was active.
        After releasing the writer, enforces MAX_RECORDINGS by deleting the oldest files.
        """
        if self._writer is None:
            log.debug("stop() called but no recording is active — no-op")
            return None

        self._writer.release()
        self._writer = None

        path             = self._video_path
        self._video_path = None

        log.info(
            "Recording stopped — path=%s  frames_written=%d  frames_skipped=%d",
            path, self._frames_written, self._frames_skipped,
        )
        self._enforce_limit()
        return path

    # ── Internal ──────────────────────────────────────────────────────────────

    def _enforce_limit(self) -> None:
        """Delete oldest session_*.mp4 files until only MAX_RECORDINGS remain."""
        files = sorted(
            RECORDINGS_DIR.glob("session_*.mp4"),
            key=lambda p: p.stat().st_mtime,
        )
        while len(files) > MAX_RECORDINGS:
            oldest = files.pop(0)
            try:
                oldest.unlink()
                log.info("Deleted old recording: %s", oldest)
            except OSError as exc:
                log.warning("Failed to delete old recording %s: %s", oldest, exc)
