"""analyzer.py — Ollama vision calls for FormCheck.

Two analysis modes:
  analyze_form(frame)       — called every ~45 s during a session.
                              Returns form/posture issues only (severity, tip).
  analyze_summary(frames)   — called once after a session ends.
                              Identifies exercises and estimates rep counts from
                              a handful of sampled keyframes.
"""
import base64
import json
import time
from dataclasses import dataclass, field

import cv2
import ollama

from logger import get_logger

log = get_logger(__name__)

_MODEL = "llama3.2-vision:11b"

_FORM_SYSTEM = (
    "You are a form coach. Check only posture and alignment.\n"
    "Return JSON: {\"issues\":[], \"severity\":\"OK\", \"tip\":\"\"}.\n"
    "Severity: OK=correct form, WARNING=visible deviation, CRITICAL=injury risk.\n"
    "Be critical — flag any misalignment you can see."
)

_SUMMARY_SYSTEM = (
    "You are a fitness coach reviewing exercise frames from a completed session.\n"
    "Identify what exercises were performed. For each exercise, estimate how many\n"
    "total reps were completed during the session.\n"
    "Return JSON only: {\"exercises\":[{\"name\":\"Push-Up\",\"estimated_reps\":12}]}"
)


@dataclass
class FormCheckResult:
    issues:   list = field(default_factory=list)
    severity: str  = "OK"
    tip:      str  = ""


@dataclass
class SummaryResult:
    exercises:  list = field(default_factory=list)   # [{"name": str, "estimated_reps": int}]
    total_reps: int  = 0


def _encode_frame(frame) -> str:
    """Encode a BGR numpy frame as a base64 JPEG string."""
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
    if not ok:
        raise RuntimeError(f"cv2.imencode failed (frame.shape={frame.shape})")
    return base64.b64encode(buf.tobytes()).decode("utf-8")


class Analyzer:
    def __init__(self):
        self._form_calls    = 0
        self._error_count   = 0
        self._total_latency = 0.0
        log.info("Analyzer initialised — model=%s", _MODEL)

    # ── Form analysis (called during session) ─────────────────────────────────

    def analyze_form(self, frame) -> FormCheckResult:
        """Check posture/alignment for a single frame.

        Returns FormCheckResult(issues, severity, tip).
        Called from run_in_executor — does NOT block the asyncio loop.
        """
        self._form_calls += 1
        call_n = self._form_calls

        try:
            b64 = _encode_frame(frame)
        except Exception as exc:
            log.error("analyze_form #%d — encode failed: %s", call_n, exc)
            self._error_count += 1
            return FormCheckResult()

        log.debug(
            "analyze_form #%d — b64_chars=%d  sending to %s",
            call_n, len(b64), _MODEL,
        )

        t_start  = time.time()
        raw_text = ""
        try:
            response = ollama.chat(
                model=_MODEL,
                messages=[
                    {"role": "system", "content": _FORM_SYSTEM},
                    {
                        "role":    "user",
                        "content": "Check the form in this exercise frame and reply with JSON only.",
                        "images":  [b64],
                    },
                ],
                options={"temperature": 0.0},
            )
            latency          = time.time() - t_start
            self._total_latency += latency

            raw_text = response.message.content.strip()
            # Strip markdown fences if model wraps the JSON
            text = raw_text
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]

            parsed = json.loads(text)
            result = FormCheckResult(
                issues   = parsed.get("issues", []),
                severity = parsed.get("severity", "OK"),
                tip      = parsed.get("tip", ""),
            )

            log.info(
                "analyze_form #%d — severity=%s issues=%s latency=%.1fs",
                call_n, result.severity,
                result.issues if result.issues else "none",
                latency,
            )
            if result.severity in ("WARNING", "CRITICAL"):
                log.warning(
                    "analyze_form #%d — %s: %s | tip: %s",
                    call_n, result.severity,
                    ", ".join(result.issues), result.tip,
                )
            return result

        except json.JSONDecodeError as exc:
            latency = time.time() - t_start
            self._error_count += 1
            log.error(
                "analyze_form #%d — JSON parse error after %.1fs: %s | raw=%r",
                call_n, latency, exc, raw_text[:200],
            )
            return FormCheckResult()

        except Exception as exc:
            latency = time.time() - t_start
            self._error_count += 1
            log.error(
                "analyze_form #%d — Ollama error after %.1fs: %s (%s)",
                call_n, latency, exc, type(exc).__name__,
            )
            return FormCheckResult()

    # ── Post-session summary (called after session ends) ──────────────────────

    def analyze_summary(self, frames: list) -> SummaryResult:
        """Identify exercises and estimate reps from a list of keyframes.

        Returns SummaryResult(exercises, total_reps).
        Called from run_in_executor — does NOT block the asyncio loop.
        If frames is empty, returns SummaryResult() without any Ollama call.
        """
        if not frames:
            log.info("analyze_summary — no frames provided, returning empty result")
            return SummaryResult()

        log.info("analyze_summary — encoding %d keyframes for %s", len(frames), _MODEL)
        images = []
        for i, frame in enumerate(frames):
            try:
                images.append(_encode_frame(frame))
            except Exception as exc:
                log.warning("analyze_summary — frame %d encode failed: %s", i, exc)

        if not images:
            log.warning("analyze_summary — all frames failed to encode")
            return SummaryResult()

        t_start  = time.time()
        raw_text = ""
        try:
            response = ollama.chat(
                model=_MODEL,
                messages=[
                    {"role": "system", "content": _SUMMARY_SYSTEM},
                    {
                        "role":    "user",
                        "content": "Review these frames from a completed workout session and reply with JSON only.",
                        "images":  images,
                    },
                ],
                options={"temperature": 0.0},
            )
            latency  = time.time() - t_start
            raw_text = response.message.content.strip()

            text = raw_text
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]

            parsed     = json.loads(text)
            exercises  = parsed.get("exercises", [])
            total_reps = sum(e.get("estimated_reps", 0) for e in exercises)

            result = SummaryResult(exercises=exercises, total_reps=total_reps)
            log.info(
                "analyze_summary — exercises=%s total_reps=%d latency=%.1fs",
                [e.get("name") for e in exercises], total_reps, latency,
            )
            return result

        except json.JSONDecodeError as exc:
            latency = time.time() - t_start
            self._error_count += 1
            log.error(
                "analyze_summary — JSON parse error after %.1fs: %s | raw=%r",
                latency, exc, raw_text[:200],
            )
            return SummaryResult()

        except Exception as exc:
            latency = time.time() - t_start
            self._error_count += 1
            log.error(
                "analyze_summary — Ollama error after %.1fs: %s (%s)",
                latency, exc, type(exc).__name__,
            )
            return SummaryResult()
