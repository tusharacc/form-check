import subprocess
from typing import Optional
import logging

class WakeLock:
    def __init__(self):
        self.process = None
        logger = logging.getLogger(__name__)

    def acquire(self) -> bool:
        try:
            self.process = subprocess.Popen(['caffeinate', '-i'])
            return True
        except Exception as e:
            logger.error(f"Failed to acquire wake lock: {e}")
            return False

    def release(self):
        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            finally:
                self.process = None
