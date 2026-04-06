"""recorder.py — Session video recording for FormCheck.

Writes annotated frames to an MP4 file during each session.
Enforces a maximum of MAX_RECORDINGS files; oldest are deleted automatically.
"""
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
        recorder.write_frame(frame)      # call each frame
        final_path = recorder.stop()     # releases writer, enforces 7-file limit
    """

    def __init__(self, width: int, height: int, fps: int = 15) -> None:
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        self.width  = width
        self.height = height
        self.fps    = fps

        self._writer: cv2.VideoWriter | None = None
        self._video_path: Path | None        = None

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

        self._writer     = cv2.VideoWriter(str(path), fourcc, self.fps, (self.width, self.height))
        self._video_path = path

        log.info("Recording started — path=%s", path)
        return path

    def write_frame(self, frame: np.ndarray) -> None:
        """Write a single BGR frame to the active recording.

        No-op if start() has not been called.
        """
        if self._writer is not None:
            self._writer.write(frame)

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

        log.info("Recording stopped — path=%s", path)
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
