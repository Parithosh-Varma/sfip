"""SQLite persistence layer: investigations, papers, figures, candidates,
evidence, jobs, events, reports, and budget counters.

Everything the controller needs to observe, act, and recover-after-crash lives
here — this is what makes the autonomous loop resumable.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS investigations (
  id INTEGER PRIMARY KEY,
  seed TEXT NOT NULL,
  seed_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  created_at TEXT, updated_at TEXT, started_at TEXT, finished_at TEXT,
  config_json TEXT,
  state_json TEXT DEFAULT '{}',
  error TEXT
);

CREATE TABLE IF NOT EXISTS papers (
  id INTEGER PRIMARY KEY,
  investigation_id INTEGER NOT NULL,
  doi TEXT, external_id TEXT,
  title TEXT, authors TEXT, year INTEGER, venue TEXT,
  source TEXT, oa_url TEXT, pdf_url TEXT, landing_url TEXT,
  pdf_path TEXT, pdf_bytes INTEGER DEFAULT 0,
  n_figures INTEGER DEFAULT 0,
  status TEXT DEFAULT 'discovered',
  relevance REAL DEFAULT 0.0,
  metadata_json TEXT,
  added_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_inv_ext ON papers(investigation_id, external_id);
CREATE INDEX IF NOT EXISTS idx_papers_inv ON papers(investigation_id);

CREATE TABLE IF NOT EXISTS figures (
  id INTEGER PRIMARY KEY,
  investigation_id INTEGER NOT NULL,
  paper_id INTEGER NOT NULL,
  page INTEGER, idx_in_page INTEGER,
  width INTEGER, height INTEGER, sha256 TEXT,
  phash TEXT, dhash TEXT, ahash TEXT,
  caption TEXT, panel_labels TEXT,
  image_path TEXT, kind TEXT DEFAULT 'raster',
  added_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_figures_paper_sha ON figures(paper_id, sha256);
CREATE INDEX IF NOT EXISTS idx_figures_inv ON figures(investigation_id);

CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY,
  investigation_id INTEGER NOT NULL,
  figure_a INTEGER NOT NULL, figure_b INTEGER NOT NULL,
  pair_key TEXT UNIQUE,
  stage TEXT DEFAULT 'gate', status TEXT DEFAULT 'open',
  verdict TEXT, score REAL, best_transform TEXT,
  signals_json TEXT, uncertainty REAL, flags_json TEXT,
  interpretation_json TEXT, llm_used INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_inv ON candidates(investigation_id);

CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY,
  investigation_id INTEGER NOT NULL,
  subject_type TEXT NOT NULL, subject_id INTEGER NOT NULL,
  kind TEXT, method TEXT, params_json TEXT, value TEXT,
  interpretation TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_subject ON evidence(subject_type, subject_id);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  investigation_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT,
  dedupe_key TEXT,
  status TEXT DEFAULT 'pending',
  priority INTEGER DEFAULT 0,
  attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3,
  run_after TEXT, last_error TEXT,
  created_at TEXT, updated_at TEXT,
  UNIQUE(investigation_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, priority DESC, id);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  investigation_id INTEGER, ts TEXT,
  level TEXT DEFAULT 'info', actor TEXT,
  message TEXT, data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_inv ON events(investigation_id, id);

CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY,
  investigation_id INTEGER NOT NULL,
  path TEXT, format TEXT, summary TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS counters (
  investigation_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  value REAL DEFAULT 0,
  PRIMARY KEY (investigation_id, name)
);
"""

_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(settings.db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    settings.ensure_dirs()
    with _lock, connect() as conn:
        conn.executescript(SCHEMA)


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _lock, connect() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """Run a write statement; return lastrowid."""
    with _lock, connect() as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.lastrowid or cur.rowcount


def executescript(script: str) -> None:
    with _lock, connect() as conn:
        conn.executescript(script)


# --- investigations -----------------------------------------------------------

def create_investigation(seed: str, seed_type: str, config_snapshot: dict) -> int:
    ts = now_iso()
    inv_id = execute(
        "INSERT INTO investigations(seed, seed_type, status, created_at, updated_at, started_at, config_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (seed, seed_type, "running", ts, ts, ts, json.dumps(config_snapshot)),
    )
    add_event(inv_id, "info", "controller", f"Investigation created for seed {seed!r} ({seed_type})", {})
    return inv_id


def get_investigation(inv_id: int) -> Optional[dict]:
    row = query_one("SELECT * FROM investigations WHERE id=?", (inv_id,))
    if row:
        row["state"] = json.loads(row.get("state_json") or "{}")
        row["config"] = json.loads(row.get("config_json") or "{}")
    return row


def list_investigations() -> list[dict]:
    rows = query("SELECT * FROM investigations ORDER BY id DESC")
    for r in rows:
        r["state"] = json.loads(r.get("state_json") or "{}")
    return rows


def set_investigation_status(inv_id: int, status: str, error: Optional[str] = None) -> None:
    finished = now_iso() if status in ("completed", "stopped_budget", "stopped_runtime", "error") else None
    if finished:
        execute(
            "UPDATE investigations SET status=?, error=?, updated_at=?, finished_at=? WHERE id=?",
            (status, error, now_iso(), finished, inv_id),
        )
    else:
        execute("UPDATE investigations SET status=?, error=?, updated_at=? WHERE id=?",
                (status, error, now_iso(), inv_id))


def set_inv_state(inv_id: int, state: dict) -> None:
    execute("UPDATE investigations SET state_json=?, updated_at=? WHERE id=?",
            (json.dumps(state), now_iso(), inv_id))


# --- counters / budgets -------------------------------------------------------

def get_counter(inv_id: int, name: str) -> float:
    row = query_one("SELECT value FROM counters WHERE investigation_id=? AND name=?", (inv_id, name))
    return float(row["value"]) if row else 0.0


def bump_counter(inv_id: int, name: str, delta: float = 1.0) -> float:
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO counters(investigation_id, name, value) VALUES (?,?,?) "
            "ON CONFLICT(investigation_id, name) DO UPDATE SET value = value + ?",
            (inv_id, name, delta, delta),
        )
        row = conn.execute("SELECT value FROM counters WHERE investigation_id=? AND name=?",
                           (inv_id, name)).fetchone()
        return float(row["value"])


def all_counters(inv_id: int) -> dict:
    return {r["name"]: r["value"] for r in query("SELECT name, value FROM counters WHERE investigation_id=?", (inv_id,))}


def set_counter(inv_id: int, name: str, value: float) -> None:
    execute(
        "INSERT INTO counters(investigation_id, name, value) VALUES (?,?,?) "
        "ON CONFLICT(investigation_id, name) DO UPDATE SET value=excluded.value",
        (inv_id, name, value),
    )


# --- events / audit log -------------------------------------------------------

def add_event(inv_id: Optional[int], level: str, actor: str, message: str, data: Optional[dict] = None) -> None:
    execute("INSERT INTO events(investigation_id, ts, level, actor, message, data_json) VALUES (?,?,?,?,?,?)",
            (inv_id, now_iso(), level, actor, message[:4000], json.dumps(data or {})))


# --- jobs ---------------------------------------------------------------------

def enqueue_job(inv_id: int, kind: str, payload: Optional[dict] = None,
                priority: int = 0, dedupe_key: Optional[str] = None,
                run_after: Optional[str] = None, max_attempts: Optional[int] = None) -> Optional[int]:
    """Idempotent enqueue (dedupe_key unique per investigation). Returns job id or None if duplicate."""
    try:
        return execute(
            "INSERT INTO jobs(investigation_id, kind, payload_json, dedupe_key, status, priority, "
            "max_attempts, run_after, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (inv_id, kind, json.dumps(payload or {}), dedupe_key, "pending",
             priority, max_attempts or settings.max_job_attempts, run_after, now_iso(), now_iso()),
        )
    except Exception:
        return None


def claim_next_job(inv_id: int) -> Optional[dict]:
    """Atomically claim the highest-priority runnable job."""
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE investigation_id=? AND status='pending' "
            "AND (run_after IS NULL OR run_after<=?) "
            "ORDER BY priority DESC, id ASC LIMIT 1",
            (inv_id, now_iso()),
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                     (now_iso(), row["id"]))
        job = dict(row)
        job["attempts"] += 1
        job["payload"] = json.loads(job.pop("payload_json") or "{}")
        return job


def finish_job(job_id: int, ok: bool, error: Optional[str] = None) -> None:
    status = "done" if ok else "failed"
    execute("UPDATE jobs SET status=?, last_error=?, updated_at=? WHERE id=?",
            (status, error, now_iso(), job_id))


def requeue_job(job_id: int, run_after: str) -> None:
    execute("UPDATE jobs SET status='pending', run_after=?, updated_at=? WHERE id=?",
            (run_after, now_iso(), job_id))


def release_stuck_jobs(inv_id: Optional[int] = None) -> int:
    """On startup: jobs left 'running' by a crash go back to pending (resume)."""
    if inv_id is None:
        rows = query("SELECT id FROM jobs WHERE status='running'")
    else:
        rows = query("SELECT id FROM jobs WHERE status='running' AND investigation_id=?", (inv_id,))
    for r in rows:
        execute("UPDATE jobs SET status='pending', updated_at=? WHERE id=?", (now_iso(), r["id"]))
    return len(rows)


def queue_stats(inv_id: int) -> dict:
    rows = query("SELECT status, COUNT(*) AS n FROM jobs WHERE investigation_id=? GROUP BY status", (inv_id,))
    return {r["status"]: r["n"] for r in rows}


# --- generic entity helpers ---------------------------------------------------

def insert_figure(inv_id: int, paper_id: int, **fields) -> Optional[int]:
    cols = ["investigation_id", "paper_id"]
    vals: list[Any] = [inv_id, paper_id]
    for k, v in fields.items():
        cols.append(k)
        vals.append(v)
    try:
        return execute(f"INSERT INTO figures({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
    except Exception:
        return None  # duplicate (paper_id, sha256)


def insert_evidence(inv_id: int, subject_type: str, subject_id: int, kind: str,
                    method: str, value: Any, interpretation: str = "",
                    params: Optional[dict] = None) -> int:
    return execute(
        "INSERT INTO evidence(investigation_id, subject_type, subject_id, kind, method, params_json, "
        "value, interpretation, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (inv_id, subject_type, subject_id, kind, method, json.dumps(params or {}),
         json.dumps(value) if not isinstance(value, str) else value, interpretation, now_iso()),
    )
