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
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


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
