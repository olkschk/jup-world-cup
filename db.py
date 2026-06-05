"""MongoDB access layer for the wallets collection."""
from __future__ import annotations

import logging
from typing import Any, Iterable

from pymongo import MongoClient
from pymongo.collection import Collection

from config import MONGO_COLLECTION, MONGO_DB, MONGO_URI

logger = logging.getLogger(__name__)

STATUS_DONE = "DONE"
STATUS_LOW_BALANCE = "LOW BALANCE"
STATUS_ERROR_PREFIX = "ERROR"  # actual stored value is "ERROR: <details>"
STATUS_PENDING = "PENDING"

_client: MongoClient | None = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI)
    return _client


def wallets() -> Collection:
    coll = get_client()[MONGO_DB][MONGO_COLLECTION]
    coll.create_index("address", unique=True)
    return coll


def upsert_wallet(address: str, encrypted_privkey: str) -> None:
    wallets().update_one(
        {"address": address},
        {
            "$setOnInsert": {
                "address": address,
                "privatekey": encrypted_privkey,
                "status": STATUS_PENDING,
                "user_ref": False,
            }
        },
        upsert=True,
    )


def set_status(address: str, status: str, user_ref: bool | None = None) -> None:
    update: dict[str, Any] = {"status": status}
    if user_ref is not None:
        update["user_ref"] = user_ref
    wallets().update_one({"address": address}, {"$set": update})


def set_user_ref(address: str) -> None:
    """Mark user_ref=True immediately, without touching the status field."""
    wallets().update_one({"address": address}, {"$set": {"user_ref": True}})


def set_error(address: str, details: str) -> None:
    set_status(address, f"{STATUS_ERROR_PREFIX}: {details}"[:500])


def pending_wallets() -> Iterable[dict[str, Any]]:
    """Wallets eligible to (re)try: everything except DONE."""
    return wallets().find({"status": {"$not": {"$regex": f"^{STATUS_DONE}$"}}})
