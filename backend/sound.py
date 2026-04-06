import numpy as np
import pygame

from logger import get_logger

log = get_logger(__name__)


def _generate_sine_wave(
    frequency: float, duration: float, sample_rate: int = 44100
) -> np.ndarray:
    """Generate a stereo int16 sine wave array suitable for pygame.sndarray.make_sound().

    Returns shape (N, 2) int16 — two identical channels (mono signal in stereo).
    """
    t    = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
    mono = (np.sin(2 * np.pi * frequency * t) * 32767).astype(np.int16)
    return np.column_stack([mono, mono])   # stereo: (N, 2)


class SoundEngine:
    def __init__(self):
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
        self._alert_count = 0
        log.info("SoundEngine initialised (pygame mixer ready)")

    def alert(self, severity: str) -> None:
        self._alert_count += 1
        log.info("Sound alert #%d — severity=%s", self._alert_count, severity)
        if severity == "WARNING":
            self.play_beep(440, 0.3)
        elif severity == "CRITICAL":
            self.play_beep(880, 0.2)
            self.play_beep(880, 0.2)
        else:
            log.debug("alert() called with unrecognised severity=%r — no sound", severity)

    def play_beep(self, frequency: float, duration: float) -> None:
        log.debug("play_beep — freq=%.0fHz  duration=%.2fs", frequency, duration)
        try:
            pygame.mixer.Sound("assets/alert.wav").play()
            log.debug("play_beep — used assets/alert.wav")
        except pygame.error as e:
            log.debug("play_beep — alert.wav unavailable (%s), generating sine wave", e)
            beep  = _generate_sine_wave(frequency, duration)
            sound = pygame.sndarray.make_sound(beep)
            sound.play()
