"""SQLite storage. One file under $DATA_DIR, opened per request."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "reader.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS texts (
    id          INTEGER PRIMARY KEY,
    lang        TEXT NOT NULL,
    title       TEXT NOT NULL,
    source_url  TEXT,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    opened_at   TEXT
);

-- Sentence boundaries are character offsets into texts.body.
CREATE TABLE IF NOT EXISTS sentences (
    id          INTEGER PRIMARY KEY,
    text_id     INTEGER NOT NULL REFERENCES texts(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    start       INTEGER NOT NULL,
    end         INTEGER NOT NULL,
    translation TEXT,
    UNIQUE (text_id, idx)
);

-- One row per dictionary form. Survives deletion of the texts it came from.
CREATE TABLE IF NOT EXISTS lemmas (
    id              INTEGER PRIMARY KEY,
    lang            TEXT NOT NULL,
    lemma           TEXT NOT NULL,
    pos             TEXT NOT NULL DEFAULT '',
    stressed        TEXT,
    gloss           TEXT,
    other_meanings  TEXT,          -- JSON array
    compound_parts  TEXT,          -- JSON array
    cefr            TEXT,
    cefr_source     TEXT,
    notes           TEXT,
    status          TEXT NOT NULL DEFAULT 'new',   -- new | learning | known
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (lang, lemma, pos)
);

-- Each place a word was looked up. Keeps its sentence so the example
-- survives if the text is deleted.
CREATE TABLE IF NOT EXISTS occurrences (
    id                INTEGER PRIMARY KEY,
    lemma_id          INTEGER NOT NULL REFERENCES lemmas(id) ON DELETE CASCADE,
    text_id           INTEGER REFERENCES texts(id) ON DELETE SET NULL,
    sentence_id       INTEGER REFERENCES sentences(id) ON DELETE SET NULL,
    surface           TEXT NOT NULL,
    morphology        TEXT,
    gloss_in_context  TEXT,
    sentence_text     TEXT,
    start             INTEGER NOT NULL,
    end               INTEGER NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (text_id, start, end)
);

CREATE TABLE IF NOT EXISTS phrases (
    id            INTEGER PRIMARY KEY,
    lang          TEXT NOT NULL,
    text_id       INTEGER REFERENCES texts(id) ON DELETE SET NULL,
    start         INTEGER NOT NULL,
    end           INTEGER NOT NULL,
    span          TEXT NOT NULL,
    context       TEXT,
    translation   TEXT,
    literal       TEXT,
    notes         TEXT,
    kind          TEXT NOT NULL DEFAULT 'selection',  -- selection | sentence
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (text_id, start, end)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Every backend answer, keyed on (task, backend, model, input), so re-reading
-- a text never pays twice.
CREATE TABLE IF NOT EXISTS llm_cache (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per backend call (and per cache hit), for the usage & costs view.
CREATE TABLE IF NOT EXISTS usage (
    id             INTEGER PRIMARY KEY,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    task           TEXT NOT NULL,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    chars          INTEGER NOT NULL DEFAULT 0,
    cached         INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL
);

CREATE INDEX IF NOT EXISTS idx_usage_time ON usage(created_at);
-- Follow-up conversations about a word (kind 'word', ref = lemma id) or a
-- phrase (kind 'phrase', ref = phrase id). Kept so answers are never paid for twice.
CREATE TABLE IF NOT EXISTS chats (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    ref_id      INTEGER NOT NULL,
    lang        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (kind, ref_id)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id             INTEGER PRIMARY KEY,
    chat_id        INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role           TEXT NOT NULL,          -- user | assistant
    content        TEXT NOT NULL,
    model          TEXT,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Flashcard scheduling (FSRS), one row per word that has been reviewed.
-- Words without a row are new cards. Times are UTC 'YYYY-MM-DD HH:MM:SS'.
CREATE TABLE IF NOT EXISTS review_state (
    lemma_id     INTEGER PRIMARY KEY REFERENCES lemmas(id) ON DELETE CASCADE,
    due          TEXT NOT NULL,
    stability    REAL NOT NULL,
    difficulty   REAL NOT NULL,
    reps         INTEGER NOT NULL DEFAULT 0,
    lapses       INTEGER NOT NULL DEFAULT 0,
    last_review  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_log (
    id           INTEGER PRIMARY KEY,
    lemma_id     INTEGER REFERENCES lemmas(id) ON DELETE SET NULL,
    lang         TEXT NOT NULL,
    rating       INTEGER NOT NULL,          -- 1 again, 2 hard, 3 good, 4 easy
    was_new      INTEGER NOT NULL DEFAULT 0,
    interval_days REAL,
    reviewed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Summaries of a text: kind 'target' (in the learner's language) or
-- 'simple' (in the text's own language, simplified).
CREATE TABLE IF NOT EXISTS summaries (
    text_id     INTEGER NOT NULL REFERENCES texts(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    summary     TEXT NOT NULL,
    key_points  TEXT,          -- JSON array
    model       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (text_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_review_due ON review_state(due);
CREATE INDEX IF NOT EXISTS idx_review_log_time ON review_log(reviewed_at);
CREATE INDEX IF NOT EXISTS idx_chat_messages ON chat_messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_sentences_text ON sentences(text_id);
CREATE INDEX IF NOT EXISTS idx_occ_lemma ON occurrences(lemma_id);
CREATE INDEX IF NOT EXISTS idx_occ_text ON occurrences(text_id);
CREATE INDEX IF NOT EXISTS idx_phrases_text ON phrases(text_id);
CREATE INDEX IF NOT EXISTS idx_lemmas_lang ON lemmas(lang, status);
"""


def _ulower(s):
    # SQLite's lower()/LIKE only fold ASCII; Cyrillic and ä/ö need this.
    return s.lower() if isinstance(s, str) else s


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.create_function("ulower", 1, _ulower, deterministic=True)
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring databases created by earlier versions up to the current schema."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(phrases)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE phrases ADD COLUMN kind TEXT NOT NULL DEFAULT 'selection'")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(lemmas)")}
    if "starred" not in cols:
        conn.execute("ALTER TABLE lemmas ADD COLUMN starred INTEGER NOT NULL DEFAULT 0")
