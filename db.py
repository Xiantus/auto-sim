"""db.py — SQLite persistence for users and characters."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "autosim.db"


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    NOT NULL UNIQUE,
                password_hash TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS characters (
                id                        TEXT    NOT NULL,
                user_id                   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name                      TEXT    NOT NULL,
                spec                      TEXT    NOT NULL,
                spec_id                   INTEGER NOT NULL DEFAULT 63,
                loot_spec_id              INTEGER NOT NULL DEFAULT 63,
                region                    TEXT    NOT NULL DEFAULT 'eu',
                realm                     TEXT    NOT NULL DEFAULT '',
                crafted_stats             TEXT    NOT NULL DEFAULT '36/49',
                simc_string               TEXT    NOT NULL DEFAULT '',
                ilvl                      REAL,
                exclude_from_item_updates INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (id, user_id)
            )
        """)


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

def create_user(username: str, password_hash: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, password_hash),
        )
        return cur.lastrowid


def get_user_by_username(username: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Character helpers
# ---------------------------------------------------------------------------

def _row_to_char(row) -> dict:
    d = dict(row)
    d["exclude_from_item_updates"] = bool(d["exclude_from_item_updates"])
    d.pop("user_id", None)
    return d


def load_characters(user_id: int) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM characters WHERE user_id = ? ORDER BY name, spec",
            (user_id,),
        ).fetchall()
    return [_row_to_char(r) for r in rows]


def load_all_characters() -> list:
    """Super-user load — returns every character across all accounts (used by Discord bot)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM characters ORDER BY name, spec"
        ).fetchall()
    return [_row_to_char(r) for r in rows]


def upsert_character(user_id: int, char: dict) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO characters
                (id, user_id, name, spec, spec_id, loot_spec_id, region, realm,
                 crafted_stats, simc_string, ilvl, exclude_from_item_updates)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id, user_id) DO UPDATE SET
                name                      = excluded.name,
                spec                      = excluded.spec,
                spec_id                   = excluded.spec_id,
                loot_spec_id              = excluded.loot_spec_id,
                region                    = excluded.region,
                realm                     = excluded.realm,
                crafted_stats             = excluded.crafted_stats,
                simc_string               = excluded.simc_string,
                ilvl                      = excluded.ilvl,
                exclude_from_item_updates = excluded.exclude_from_item_updates
        """, (
            char["id"], user_id,
            char["name"], char["spec"],
            char.get("spec_id", 63), char.get("loot_spec_id", 63),
            char.get("region", "eu"), char.get("realm", ""),
            char.get("crafted_stats", "36/49"),
            char.get("simc_string", ""),
            char.get("ilvl"),
            int(bool(char.get("exclude_from_item_updates", False))),
        ))


def update_character_ilvl(user_id: int, char_id: str, ilvl: float) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE characters SET ilvl = ? WHERE id = ? AND user_id = ?",
            (ilvl, char_id, user_id),
        )


def delete_character(user_id: int, char_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM characters WHERE id = ? AND user_id = ?",
            (char_id, user_id),
        )
