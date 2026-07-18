"""SQLite connection + schema bootstrap. Standard library only."""
from __future__ import annotations

import datetime as _dt
import pathlib
import sqlite3

import config

ROOT = pathlib.Path(__file__).parent
DB_PATH = pathlib.Path(config.get("BSA_DB_PATH"))
SCHEMA_PATH = ROOT / "schema.sql"


def now() -> str:
    """UTC timestamp, ISO 8601. Every fact in this DB is stamped with one."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def connect(path: pathlib.Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    # Views must track schema edits, but CREATE VIEW IF NOT EXISTS silently
    # keeps a stale definition. Dropping first makes schema.sql authoritative.
    conn.execute("DROP VIEW IF EXISTS business_fact")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for databases created by an earlier schema.

    CREATE TABLE IF NOT EXISTS will not add a column to a table that already
    exists, so new columns need an explicit ALTER on existing databases.
    """
    def columns(table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    for table, col, decl in (
        ("extraction", "active_web_query_description", "TEXT"),
        ("extraction", "confidence", "TEXT"),
        ("extraction", "is_chain_page", "INTEGER"),
        ("business", "place_id", "INTEGER REFERENCES place(id)"),
    ):
        if col not in columns(table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def upsert_source(conn: sqlite3.Connection, name: str, url: str,
                  license_: str, attribution: str) -> int:
    conn.execute(
        """INSERT INTO source (name, url, license, attribution) VALUES (?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             url=excluded.url, license=excluded.license,
             attribution=excluded.attribution""",
        (name, url, license_, attribution),
    )
    conn.commit()
    return conn.execute("SELECT id FROM source WHERE name=?", (name,)).fetchone()["id"]
