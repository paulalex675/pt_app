"""SQLite storage for clients, sessions and HR samples.

Designed to be a drop-in local store now, and an easy upload source to S3
later (each finished session can be exported as one JSON/CSV blob keyed by
session_id for the bronze layer).
"""
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "pt_studio.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id TEXT PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    sex TEXT,                  -- 'male' | 'female' | 'other' | NULL
    dob TEXT,                  -- ISO date, nullable
    height_cm REAL,
    weight_kg REAL,
    resting_hr INTEGER,        -- nullable, bpm
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES clients(id),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    strap_device_id TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS hr_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    ts TEXT NOT NULL,          -- ISO timestamp, UTC
    hr INTEGER NOT NULL,
    rr_intervals TEXT          -- JSON-encoded list of ms floats, nullable
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# ---------- clients ----------

def create_client(first_name, last_name, sex=None, dob: str | None = None,
                   height_cm: float | None = None, weight_kg: float | None = None,
                   resting_hr: int | None = None) -> str:
    client_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO clients
               (id, first_name, last_name, sex, dob, height_cm, weight_kg, resting_hr, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (client_id, first_name.strip(), last_name.strip(), sex, dob,
             height_cm, weight_kg, resting_hr, datetime.utcnow().isoformat()),
        )
    return client_id


def list_clients(search: str = "") -> list[sqlite3.Row]:
    with get_conn() as conn:
        if search:
            like = f"%{search.lower()}%"
            return conn.execute(
                """SELECT * FROM clients
                   WHERE lower(first_name || ' ' || last_name) LIKE ?
                   ORDER BY first_name, last_name""",
                (like,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM clients ORDER BY first_name, last_name"
        ).fetchall()


def get_client(client_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM clients WHERE id = ?", (client_id,)
        ).fetchone()


def client_age(dob_iso: str | None) -> int | None:
    if not dob_iso:
        return None
    dob = date.fromisoformat(dob_iso)
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


# ---------- sessions ----------

def start_session(client_id: str, strap_device_id: str | None = None) -> str:
    session_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, client_id, started_at, strap_device_id) VALUES (?, ?, ?, ?)",
            (session_id, client_id, datetime.utcnow().isoformat(), strap_device_id),
        )
    return session_id


def end_session(session_id: str, notes: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET ended_at = ?, notes = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), notes, session_id),
        )


def log_sample(session_id: str, hr: int, rr_intervals_json: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO hr_samples (session_id, ts, hr, rr_intervals) VALUES (?, ?, ?, ?)",
            (session_id, datetime.utcnow().isoformat(), hr, rr_intervals_json),
        )


def session_samples(session_id: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM hr_samples WHERE session_id = ? ORDER BY ts", (session_id,)
        ).fetchall()
