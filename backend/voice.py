import subprocess
import time

from logger import get_logger

log = get_logger(__name__)


class VoiceEngine:
    """macOS `say`-based TTS with a cooldown to prevent spam."""

    COOLDOWN = 10  # seconds between spoken messages

    def __init__(self):
        self._last_spoken = 0.0
        self._speak_count = 0
        self._skip_count  = 0
        log.info("VoiceEngine initialised — cooldown=%ds", self.COOLDOWN)

    def speak(self, text: str) -> None:
        if not text:
            log.debug("speak() called with empty text — skipped")
            return
        now = time.time()
        since_last = now - self._last_spoken
        if since_last < self.COOLDOWN:
            self._skip_count += 1
            log.debug(
                "speak() cooldown active — skipped (%.1fs remaining) text=%r  "
                "skips_total=%d",
                self.COOLDOWN - since_last, text[:60], self._skip_count,
            )
            return
        # Non-blocking: fire-and-forget so audio doesn't stall the event loop
        subprocess.Popen(["say", text])
        self._speak_count += 1
        self._last_spoken = now
        log.info(
            "TTS spoken — text=%r  (speaks_total=%d  skips_total=%d)",
            text[:80], self._speak_count, self._skip_count,
        )
