"""SQLite database access layer for the wallets table.

Uses Python's built-in sqlite3. WAL mode enables safe concurrent writes
from multiple threads (parallel wallet processing).
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from config import DB_FILE

logger = logging.getLogger(__name__)

STATUS_DONE = "DONE"
STATUS_LOW_BALANCE = "LOW BALANCE"
STATUS_ERROR_PREFIX = "ERROR"
STATUS_PENDING = "PENDING"


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create the wallets table if it doesn't exist."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                address    TEXT PRIMARY KEY,
                privatekey TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'PENDING',
                user_ref   INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    logger.debug("DB ready: %s", DB_FILE)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def upsert_wallet(address: str, encrypted_privkey: str) -> bool:
    """Insert wallet if it doesn't exist yet. Returns True if inserted."""
    with _conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO wallets (address, privatekey, status, user_ref)
            VALUES (?, ?, 'PENDING', 0)
            ON CONFLICT(address) DO NOTHING
            """,
            (address, encrypted_privkey),
        )
        conn.commit()
        return cursor.rowcount > 0


def set_status(address: str, status: str, user_ref: bool | None = None) -> None:
    with _conn() as conn:
        if user_ref is not None:
            conn.execute(
                "UPDATE wallets SET status=?, user_ref=?, updated_at=datetime('now') WHERE address=?",
                (status, int(user_ref), address),
            )
        else:
            conn.execute(
                "UPDATE wallets SET status=?, updated_at=datetime('now') WHERE address=?",
                (status, address),
            )
        conn.commit()


def set_user_ref(address: str) -> None:
    """Persist user_ref=True immediately without touching status."""
    with _conn() as conn:
        conn.execute(
            "UPDATE wallets SET user_ref=1, updated_at=datetime('now') WHERE address=?",
            (address,),
        )
        conn.commit()


def set_error(address: str, details: str) -> None:
    set_status(address, f"{STATUS_ERROR_PREFIX}: {details}"[:500])


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def pending_wallets() -> list[dict[str, Any]]:
    """Return all wallets except DONE (eligible for processing / retry)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM wallets WHERE status != ? ORDER BY created_at",
            (STATUS_DONE,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_stats() -> dict[str, Any]:
    """Return aggregated statistics for the stats command."""
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]

        by_status = {
            row["status"]: row["cnt"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM wallets GROUP BY status ORDER BY cnt DESC"
            ).fetchall()
        }

        user_ref_count = conn.execute(
            "SELECT COUNT(*) FROM wallets WHERE user_ref=1"
        ).fetchone()[0]

        low_balance_rows = conn.execute(
            "SELECT address FROM wallets WHERE status=? ORDER BY updated_at",
            (STATUS_LOW_BALANCE,),
        ).fetchall()

        error_rows = conn.execute(
            "SELECT address, status FROM wallets WHERE status LIKE 'ERROR:%' ORDER BY updated_at",
        ).fetchall()

    return {
        "total": total,
        "by_status": by_status,
        "user_ref_count": user_ref_count,
        "low_balance_wallets": [r["address"] for r in low_balance_rows],
        "error_wallets": [(r["address"], r["status"]) for r in error_rows],
    }
