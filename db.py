"""SQLite storage for clients, sessions and HR samples.

Designed to be a drop-in local store now, and an easy upload source to S3
later (each finished session can be exported as one JSON/CSV blob keyed by
session_id for the bronze layer).
"""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

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
    max_hr INTEGER,            -- nullable, highest observed bpm
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES clients(id),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    strap_device_id TEXT,
    notes TEXT,
    mode TEXT NOT NULL DEFAULT 'normal',   -- 'normal' | 'elite'
    mode_params TEXT                       -- JSON: elite settings plus each client's frozen zone 5 / recovered levels
);

CREATE TABLE IF NOT EXISTS client_device_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL REFERENCES clients(id),
    device_address TEXT NOT NULL,
    device_name TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(client_id, device_address)
);

CREATE TABLE IF NOT EXISTS hr_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    client_id TEXT REFERENCES clients(id),
    strap_device_id TEXT,
    ts TEXT NOT NULL,          -- ISO timestamp, UTC
    hr INTEGER NOT NULL,
    rr_intervals TEXT          -- JSON-encoded list of ms floats, nullable
);

CREATE TABLE IF NOT EXISTS elite_rounds (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    client_id TEXT NOT NULL REFERENCES clients(id),
    round_no INTEGER NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    prework_s REAL,
    work_s REAL,
    paused_s REAL,
    recovery_s REAL,
    hr_end_work INTEGER,
    hr_60s INTEGER,
    flags TEXT,                -- JSON list, e.g. ["pause_cap"]
    PRIMARY KEY (session_id, client_id, round_no)
);

CREATE TABLE IF NOT EXISTS session_phase_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    client_id TEXT NOT NULL REFERENCES clients(id),
    round_no INTEGER NOT NULL,
    phase TEXT NOT NULL,       -- prework | work | recovery | done
    ts TEXT NOT NULL           -- ISO timestamp, UTC
);

CREATE TABLE IF NOT EXISTS session_participants (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    client_id TEXT NOT NULL REFERENCES clients(id),
    strap_device_id TEXT NOT NULL,
    PRIMARY KEY (session_id, client_id),
    UNIQUE (session_id, strap_device_id)
);
"""


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        client_columns = {row["name"] for row in conn.execute("PRAGMA table_info(clients)")}
        if "max_hr" not in client_columns:
            conn.execute("ALTER TABLE clients ADD COLUMN max_hr INTEGER")
        sample_columns = {row["name"] for row in conn.execute("PRAGMA table_info(hr_samples)")}
        if "client_id" not in sample_columns:
            conn.execute("ALTER TABLE hr_samples ADD COLUMN client_id TEXT REFERENCES clients(id)")
        if "strap_device_id" not in sample_columns:
            conn.execute("ALTER TABLE hr_samples ADD COLUMN strap_device_id TEXT")
        session_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "mode" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'normal'")
        if "mode_params" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN mode_params TEXT")
        conn.execute(
            """INSERT OR IGNORE INTO session_participants (session_id, client_id, strap_device_id)
               SELECT id, client_id, strap_device_id FROM sessions
               WHERE strap_device_id IS NOT NULL"""
        )


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


def update_client_resting_hr(client_id: str, resting_hr: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE clients SET resting_hr = ? WHERE id = ?",
            (resting_hr, client_id),
        )


def update_client_max_hr(client_id: str, observed_hr: int) -> int | None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE clients SET max_hr = ?
               WHERE id = ? AND (max_hr IS NULL OR max_hr < ?)""",
            (observed_hr, client_id, observed_hr),
        )
        row = conn.execute("SELECT max_hr FROM clients WHERE id = ?", (client_id,)).fetchone()
        return row["max_hr"] if row else None


def client_age(dob_iso: str | None) -> int | None:
    if not dob_iso:
        return None
    dob = date.fromisoformat(dob_iso)
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


# ---------- sessions ----------

def start_session(client_id: str, strap_device_id: str | None = None,
                  mode: str = "normal", mode_params: dict | None = None) -> str:
    session_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sessions (id, client_id, started_at, strap_device_id, mode, mode_params)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, client_id, datetime.utcnow().isoformat(), strap_device_id,
             mode, json.dumps(mode_params) if mode_params is not None else None),
        )
    return session_id


def end_session(session_id: str, notes: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET ended_at = ?, notes = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), notes, session_id),
        )


def get_session(session_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()


def session_summary(session_id: str) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    with get_conn() as conn:
        session = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None:
            return None, []
        participants = conn.execute(
            """SELECT c.id AS client_id, c.first_name, c.last_name, c.sex, c.dob,
                      c.weight_kg,
                      MAX(h.hr) AS max_hr,
                      AVG(h.hr) AS average_hr,
                      COUNT(h.id) AS sample_count
               FROM clients AS c
               LEFT JOIN session_participants AS sp
                 ON sp.session_id = ? AND sp.client_id = c.id
               LEFT JOIN hr_samples AS h
                 ON h.session_id = ?
                AND (h.client_id = c.id OR (h.client_id IS NULL AND c.id = ?))
                AND (sp.strap_device_id IS NULL OR h.strap_device_id IS NULL
                     OR h.strap_device_id = sp.strap_device_id)
               WHERE sp.session_id = ? OR c.id = ?
               GROUP BY c.id
               ORDER BY c.first_name, c.last_name""",
            (session_id, session_id, session["client_id"], session_id, session["client_id"]),
        ).fetchall()
        return session, participants


def add_session_participant(session_id: str, client_id: str, strap_device_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO session_participants (session_id, client_id, strap_device_id)
               VALUES (?, ?, ?)""",
            (session_id, client_id, strap_device_id),
        )


def list_session_participants(session_id: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT sp.client_id, sp.strap_device_id, c.first_name, c.last_name
               FROM session_participants AS sp
               JOIN clients AS c ON c.id = sp.client_id
               WHERE sp.session_id = ? ORDER BY c.first_name, c.last_name""",
            (session_id,),
        ).fetchall()


def add_client_device_binding(client_id: str, device_address: str, device_name: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO client_device_bindings (client_id, device_address, device_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(client_id, device_address) DO UPDATE SET device_name = excluded.device_name
            """,
            (client_id, device_address, device_name, datetime.utcnow().isoformat()),
        )


def list_client_device_bindings() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM client_device_bindings ORDER BY client_id, device_address"
        ).fetchall()


def get_client_device_binding(client_id: str, device_address: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM client_device_bindings WHERE client_id = ? AND device_address = ?",
            (client_id, device_address),
        ).fetchone()


def log_sample(
    session_id: str,
    hr: int,
    rr_intervals_json: str | None,
    client_id: str | None = None,
    strap_device_id: str | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO hr_samples (session_id, client_id, strap_device_id, ts, hr, rr_intervals)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, client_id, strap_device_id, datetime.utcnow().isoformat(), hr, rr_intervals_json),
        )


def session_samples(session_id: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM hr_samples WHERE session_id = ? ORDER BY ts", (session_id,)
        ).fetchall()


# ---------- elite mode ----------

def session_mode_params(session) -> dict:
    """A session's mode settings as a dict (empty for normal sessions)."""
    raw = session["mode_params"] if session is not None else None
    return json.loads(raw) if raw else {}


def save_phase_event(session_id: str, client_id: str, round_no: int, phase: str, ts_iso: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO session_phase_events (session_id, client_id, round_no, phase, ts)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, client_id, round_no, phase, ts_iso),
        )


def save_elite_round(session_id: str, client_id: str, round_no: int, started_at: str | None,
                     ended_at: str | None, prework_s: float, work_s: float, paused_s: float,
                     recovery_s: float | None, hr_end_work: int | None, hr_60s: int | None,
                     flags: list[str]) -> None:
    """Inserts or updates one round. A round is saved when work ends and updated when recovery ends."""
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO elite_rounds
               (session_id, client_id, round_no, started_at, ended_at, prework_s, work_s,
                paused_s, recovery_s, hr_end_work, hr_60s, flags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, client_id, round_no, started_at, ended_at, prework_s, work_s,
             paused_s, recovery_s, hr_end_work, hr_60s, json.dumps(flags)),
        )


def list_elite_rounds(session_id: str, client_id: str | None = None) -> list[sqlite3.Row]:
    with get_conn() as conn:
        if client_id:
            return conn.execute(
                "SELECT * FROM elite_rounds WHERE session_id = ? AND client_id = ? ORDER BY round_no",
                (session_id, client_id),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM elite_rounds WHERE session_id = ? ORDER BY client_id, round_no",
            (session_id,),
        ).fetchall()


def list_phase_events(session_id: str, client_id: str | None = None) -> list[sqlite3.Row]:
    with get_conn() as conn:
        if client_id:
            return conn.execute(
                "SELECT * FROM session_phase_events WHERE session_id = ? AND client_id = ? ORDER BY id",
                (session_id, client_id),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM session_phase_events WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
