import sqlite3
import os
from datetime import datetime, timezone
from logger import get_logger

log = get_logger(__name__)

# DB_PATH is set by server.py before the first call; fall back to cwd for tests
DB_PATH = os.path.join(os.path.dirname(__file__), "formcheck.db")


def _conn():
    return sqlite3.connect(DB_PATH)


def _normalise(name: str | None) -> str | None:
    """Strip, collapse whitespace, title-case exercise names for consistent aggregation."""
    if not name:
        return name
    return " ".join(name.strip().split()).title()


def init_db(db_path: str | None = None) -> None:
    global DB_PATH
    if db_path:
        DB_PATH = db_path
    conn = _conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time TEXT NOT NULL,
            end_time   TEXT,
            summary    TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER REFERENCES sessions(id),
            ts         TEXT NOT NULL,
            exercise   TEXT,
            severity   TEXT,
            issue      TEXT,
            tip        TEXT
        )
    """)
    # Safe migration: add video_path column if it doesn't exist yet
    try:
        conn.cursor().execute("ALTER TABLE sessions ADD COLUMN video_path TEXT")
        conn.commit()
        log.info("journal: added video_path column to sessions table")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_session() -> int:
    conn = _conn()
    c = conn.cursor()
    c.execute("INSERT INTO sessions (start_time) VALUES (?)", [_now()])
    session_id = c.lastrowid
    conn.commit()
    conn.close()
    return session_id


def log_event(session_id, exercise, severity, issue, tip) -> None:
    conn = _conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO events (session_id, ts, exercise, severity, issue, tip) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [session_id, _now(), _normalise(exercise), severity, issue, tip],
    )
    conn.commit()
    conn.close()


def end_session(session_id: int, summary: str = "") -> None:
    conn = _conn()
    c = conn.cursor()
    c.execute(
        "UPDATE sessions SET end_time=?, summary=? WHERE id=?",
        [_now(), summary, session_id],
    )
    conn.commit()
    conn.close()


def update_session_video(session_id: int, video_path: str) -> None:
    """Store the recording path against the session row."""
    conn = _conn()
    c = conn.cursor()
    c.execute("UPDATE sessions SET video_path=? WHERE id=?", [video_path, session_id])
    conn.commit()
    conn.close()
    log.info("journal: session_id=%d video_path set to %s", session_id, video_path)


def get_sessions() -> list:
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT id, start_time, end_time, summary FROM sessions ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return rows
