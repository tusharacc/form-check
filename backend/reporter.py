"""reporter.py — Workout history aggregation and next-week plan generator.

Queries the SQLite journal for the past N days, categorises every exercise
into a body-part group (Upper Body / Lower Body / Core / Cardio / General),
then asks Ollama to generate a balanced 5-day plan for next week.

Public API
----------
generate_report(days=30) -> dict
    Returns a fully-structured report dict ready to JSON-serialise and send
    to the renderer.  Blocking — always call via run_in_executor.
"""
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ollama

from logger import get_logger

log = get_logger(__name__)

_MODEL   = "llama3.2-vision:11b"
_DB_PATH = str(Path(__file__).parent / "formcheck.db")

# ── Body-part keyword mapping ─────────────────────────────────────────────────

_CATEGORIES = {
    "Upper Body": [
        "push-up", "pushup", "push up",
        "pull-up", "pullup", "pull up",
        "shoulder press", "overhead press", "military press",
        "bicep curl", "hammer curl", "tricep", "dip",
        "bench press", "chest fly", "chest press",
        "row", "lat pulldown", "face pull", "upright row",
        "lateral raise", "front raise",
    ],
    "Core": [
        "plank", "crunch", "sit-up", "situp",
        "russian twist", "leg raise", "flutter kick",
        "hollow hold", "ab wheel", "bicycle crunch",
        "dead bug", "pallof",
    ],
    "Lower Body": [
        "squat", "lunge", "deadlift", "leg press",
        "calf raise", "glute bridge", "hip thrust",
        "step up", "leg curl", "leg extension",
        "romanian deadlift", "rdl", "sumo",
        "good morning", "box jump", "wall sit",
    ],
    "Cardio": [
        "run", "jog", "sprint", "cycling", "bike",
        "jumping jack", "burpee", "jump rope",
        "rowing", "elliptical", "stair",
        "high knee", "butt kick", "mountain climber",
        "skipping",
    ],
}


def _categorize(name: str) -> str:
    """Map an exercise name to a body-part category."""
    lower = name.lower()
    for category, keywords in _CATEGORIES.items():
        if any(kw in lower for kw in keywords):
            return category
    return "General"


# ── DB helpers ────────────────────────────────────────────────────────────────

def _query_history(days: int) -> list[dict]:
    """Return per-session exercise lists for the past `days` days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn   = sqlite3.connect(_DB_PATH)
    c      = conn.cursor()

    c.execute(
        "SELECT id, start_time, end_time FROM sessions "
        "WHERE start_time >= ? ORDER BY start_time ASC",
        [cutoff],
    )
    sessions = c.fetchall()

    history = []
    for sid, start, end in sessions:
        c.execute(
            "SELECT DISTINCT exercise FROM events "
            "WHERE session_id=? AND exercise IS NOT NULL AND exercise!=''",
            [sid],
        )
        exercises = [row[0] for row in c.fetchall()]

        duration_min = None
        if start and end:
            try:
                s = datetime.fromisoformat(start.replace("Z", "+00:00"))
                e = datetime.fromisoformat(end.replace("Z",   "+00:00"))
                duration_min = max(1, round((e - s).total_seconds() / 60))
            except Exception:
                pass

        history.append({
            "session_id":   sid,
            "date":         start[:10],   # YYYY-MM-DD
            "duration_min": duration_min,
            "exercises":    exercises,
        })

    conn.close()
    log.info("reporter: found %d sessions in the past %d days", len(sessions), days)
    return history


# ── Aggregation ───────────────────────────────────────────────────────────────

def _aggregate(history: list[dict]) -> dict:
    """Summarise exercise frequency and body-part coverage."""
    exercise_count  = defaultdict(int)   # name → sessions it appeared in
    category_count  = defaultdict(int)   # category → sessions
    total_minutes   = 0

    for session in history:
        seen_cats = set()
        for ex in session["exercises"]:
            exercise_count[ex] += 1
            cat = _categorize(ex)
            seen_cats.add(cat)
        for cat in seen_cats:
            category_count[cat] += 1
        if session["duration_min"]:
            total_minutes += session["duration_min"]

    # Ensure all categories appear (even with 0)
    for cat in _CATEGORIES:
        if cat not in category_count:
            category_count[cat] = 0

    return {
        "total_sessions":   len(history),
        "total_minutes":    total_minutes,
        "exercise_counts":  dict(sorted(exercise_count.items(), key=lambda x: -x[1])),
        "category_counts":  dict(category_count),
    }


# ── Ollama plan generation ────────────────────────────────────────────────────

_PLAN_SYSTEM = (
    "You are an expert fitness coach. Analyse workout history and design a "
    "balanced weekly training plan.\n"
    "Rules:\n"
    "- Max 5 training days, at least 2 rest/recovery days.\n"
    "- Avoid training the same major muscle group on consecutive days.\n"
    "- Underworked muscle groups get priority.\n"
    "- Keep exercises realistic for home or basic gym equipment.\n"
    "Return ONLY valid JSON, no markdown fences."
)

_PLAN_SCHEMA = (
    '{"plan":[{"day":"Monday","focus":"Upper Body",'
    '"exercises":["Push-Up","Shoulder Press"],'
    '"notes":"short coaching note"},...up to 7 days],'
    '"insights":"2-3 sentence summary of training balance and priorities"}'
)


def _ollama_plan(agg: dict) -> dict:
    """Call Ollama to generate the plan; returns parsed dict or fallback."""
    prompt = (
        f"Here is my workout history for the past 30 days:\n\n"
        f"Sessions: {agg['total_sessions']}\n"
        f"Total training time: {agg['total_minutes']} min\n\n"
        f"Body-part coverage (number of sessions each was trained):\n"
        + "\n".join(f"  {k}: {v} session(s)" for k, v in agg["category_counts"].items())
        + f"\n\nExercises performed (most frequent first):\n"
        + "\n".join(f"  {ex}: {cnt} session(s)" for ex, cnt in list(agg["exercise_counts"].items())[:15])
        + f"\n\nGenerate a 5-day workout plan for next week using this schema:\n{_PLAN_SCHEMA}"
    )

    log.info("reporter: calling Ollama for plan generation")
    try:
        response = ollama.chat(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _PLAN_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            options={"temperature": 0.3},
        )
        raw = response.message.content.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        log.info("reporter: Ollama plan received — %d days", len(parsed.get("plan", [])))
        return parsed
    except json.JSONDecodeError as exc:
        log.error("reporter: JSON parse error from Ollama: %s", exc)
        return {"plan": [], "insights": "Could not generate plan — JSON parse error."}
    except Exception as exc:
        log.error("reporter: Ollama error: %s", exc)
        return {"plan": [], "insights": f"Could not generate plan — {exc}"}


# ── Public API ────────────────────────────────────────────────────────────────

def generate_report(days: int = 30) -> dict:
    """Build the full weekly report and return a JSON-serialisable dict.

    Blocking — always call via loop.run_in_executor from async code.
    """
    log.info("reporter: generating report (past %d days)", days)

    history = _query_history(days)
    agg     = _aggregate(history)

    # Annotate each exercise with its body-part category for the frontend
    categorised = [
        {"name": ex, "category": _categorize(ex), "sessions": cnt}
        for ex, cnt in agg["exercise_counts"].items()
    ]

    plan_data = _ollama_plan(agg)

    report = {
        "period_days":    days,
        "total_sessions": agg["total_sessions"],
        "total_minutes":  agg["total_minutes"],
        "category_counts": agg["category_counts"],
        "exercises":      categorised,
        "plan":           plan_data.get("plan", []),
        "insights":       plan_data.get("insights", ""),
    }

    log.info(
        "reporter: report complete — sessions=%d exercises=%d plan_days=%d",
        agg["total_sessions"], len(categorised), len(report["plan"]),
    )
    return report
