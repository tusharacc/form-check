"""wakelock.py — Prevent macOS idle sleep during a FormCheck session.

Uses `caffeinate -i -w <pid>` so the lock is automatically released if the
backend process exits unexpectedly (the -w flag ties caffeinate to our PID).
"""
import os
import subprocess

from logger import get_logger

log = get_logger(__name__)


class WakeLock:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        log.info("WakeLock initialised")

    def acquire(self) -> None:
        """Spawn caffeinate to prevent idle sleep. No-op if already held."""
        if self._proc is not None:
            log.debug("WakeLock.acquire: wake lock already held")
            return
        try:
            self._proc = subprocess.Popen(["caffeinate", "-d", "-i", "-w", str(os.getpid())])
            log.info("Wake lock acquired — caffeinate pid=%d", self._proc.pid)
        except Exception as exc:
            log.warning("WakeLock.acquire failed: %s", exc)
            self._proc = None

    def release(self) -> None:
        """Terminate caffeinate. No-op if not held."""
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
            log.info("Wake lock released")
        except Exception as exc:
            log.warning("WakeLock.release error: %s", exc)
        finally:
            self._proc = None
