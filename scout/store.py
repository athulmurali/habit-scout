"""SQLite persistence: raw events, mined patterns, suggestions, run history."""
from __future__ import annotations

import json
import sqlite3
import time

from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, source TEXT, kind TEXT,
  cwd TEXT, text TEXT, session TEXT, extra TEXT,
  UNIQUE(kind, ts, text));
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS cursors(name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS patterns(
  fp TEXT PRIMARY KEY, type TEXT, key TEXT, count INTEGER, days INTEGER,
  first_seen INTEGER, last_seen INTEGER, minutes REAL, examples TEXT, updated INTEGER);
CREATE TABLE IF NOT EXISTS suggestions(
  id INTEGER PRIMARY KEY, fp TEXT, pattern_key TEXT, created INTEGER, title TEXT,
  kind TEXT, why TEXT, confidence REAL, minutes_per_week REAL, filename TEXT,
  path TEXT, install_hint TEXT, status TEXT DEFAULT 'proposed', status_ts INTEGER,
  reason TEXT, baseline_per_day REAL);
CREATE TABLE IF NOT EXISTS runs(
  day TEXT PRIMARY KEY, started INTEGER, events_added INTEGER, candidates INTEGER,
  llm_called INTEGER, suggestions INTEGER, cost_usd REAL, input_tokens INTEGER,
  output_tokens INTEGER, note TEXT);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DATA_DIR / "scout.db"))
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def get_cursor(db, name, default=None):
    row = db.execute("SELECT value FROM cursors WHERE name=?", (name,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_cursor(db, name, value):
    db.execute("INSERT OR REPLACE INTO cursors(name, value) VALUES(?, ?)", (name, json.dumps(value)))


def add_events(db, events) -> int:
    """events: iterable of (ts, source, kind, cwd, text, session, extra). Returns rows added."""
    before = db.total_changes
    db.executemany(
        "INSERT OR IGNORE INTO events(ts, source, kind, cwd, text, session, extra) VALUES(?,?,?,?,?,?,?)",
        events,
    )
    return db.total_changes - before


def prune(db, retention_days):
    db.execute("DELETE FROM events WHERE ts < ?", (int(time.time()) - retention_days * 86400,))
