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


def set_is_lost(address: str) -> None:
    """Mark wallet as having at least one losing parlay leg."""
    with _conn() as conn:
        conn.execute(
            "UPDATE wallets SET is_lost=1, updated_at=datetime('now') WHERE address=?",
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


def done_wallets() -> list[dict[str, Any]]:
    """Return all DONE wallets."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM wallets WHERE status=? ORDER BY created_at",
            (STATUS_DONE,),
        ).fetchall()
    return [dict(r) for r in rows]


def init_results_tables() -> None:
    """Create caching tables for results command and migrate wallets table."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wallet_slips (
                address    TEXT NOT NULL,
                parlay_id  TEXT NOT NULL DEFAULT '',
                event_id   TEXT NOT NULL,
                market_id  TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (address, event_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS event_scores (
                event_id   TEXT PRIMARY KEY,
                home_team  TEXT,
                away_team  TEXT,
                score      TEXT,
                status     TEXT,
                ended      INTEGER NOT NULL DEFAULT 0,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Migration: add is_lost column to existing wallets table
        try:
            conn.execute(
                "ALTER TABLE wallets ADD COLUMN is_lost INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()
    logger.debug("Results tables ready")


def get_cached_slip(address: str) -> list[dict[str, str]] | None:
    """Return cached slip legs or None if not cached yet."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT event_id, market_id FROM wallet_slips WHERE address=? ORDER BY rowid",
            (address,),
        ).fetchall()
    if not rows:
        return None
    return [{"eventId": r["event_id"], "marketId": r["market_id"]} for r in rows]


def cache_slip(address: str, parlay_id: str, legs: list[dict[str, str]]) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM wallet_slips WHERE address=?", (address,))
        for leg in legs:
            conn.execute(
                "INSERT INTO wallet_slips (address, parlay_id, event_id, market_id) VALUES (?, ?, ?, ?)",
                (address, parlay_id, leg["eventId"], leg["marketId"]),
            )
        conn.commit()


def get_cached_score(event_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM event_scores WHERE event_id=?",
            (event_id,),
        ).fetchone()
    return dict(row) if row else None


def cache_event_score(event_id: str, data: dict[str, Any]) -> None:
    with _conn() as conn:
        conn.execute("""
            INSERT INTO event_scores (event_id, home_team, away_team, score, status, ended)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                home_team  = excluded.home_team,
                away_team  = excluded.away_team,
                score      = excluded.score,
                status     = excluded.status,
                ended      = excluded.ended,
                fetched_at = datetime('now')
        """, (
            event_id,
            data.get("homeTeam"),
            data.get("awayTeam"),
            data.get("score"),
            data.get("status"),
            int(bool(data.get("ended", False))),
        ))
        conn.commit()


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
