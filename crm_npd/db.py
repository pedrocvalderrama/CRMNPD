from __future__ import annotations
import sqlite3
from pathlib import Path


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA temp_store = MEMORY;")
    conn.execute("PRAGMA cache_size = -64000;")
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def ensure_column(conn: sqlite3.Connection, table: str, column_def: str) -> bool:
    col_name = column_def.split()[0]
    cols = table_columns(conn, table)
    if col_name in cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
    return True
