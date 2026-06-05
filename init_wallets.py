"""Wallet initialisation logic (called from main.py init command).

Priority:
  1. data/seeds.txt  — BIP44 derivation from mnemonic
  2. data/privatekeys.txt — base58 private keys (fallback when seeds.txt is empty)
"""
from __future__ import annotations

import logging
import sys

from config import PRIVATE_KEYS_FILE, SEEDS_FILE
from crypto_utils import encrypt
from db import init_db, upsert_wallet
from wallet import keypair_from_mnemonic, keypair_from_secret_b58, secret_to_b58

logger = logging.getLogger(__name__)


def _read_lines(path) -> list[str]:
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def run_init() -> None:
    init_db()

    seeds = _read_lines(SEEDS_FILE)
    private_keys = _read_lines(PRIVATE_KEYS_FILE)

    if seeds:
        source, entries = "seed", seeds
        logger.info("Source: seeds.txt (%d entries)", len(entries))
    elif private_keys:
        source, entries = "privatekey", private_keys
        logger.info("Source: privatekeys.txt (%d entries)", len(entries))
    else:
        logger.error("Both seeds.txt and privatekeys.txt are empty or missing in data/")
        sys.exit(1)

    total = len(entries)
    inserted = skipped = failed = 0

    for idx, entry in enumerate(entries, 1):
        try:
            kp = keypair_from_mnemonic(entry) if source == "seed" else keypair_from_secret_b58(entry)
            address = str(kp.pubkey())
            encrypted_key = encrypt(secret_to_b58(kp))
            added = upsert_wallet(address, encrypted_key)
            if added:
                logger.info("[%d/%d] + %s", idx, total, address)
                inserted += 1
            else:
                logger.info("[%d/%d] ~ %s (already exists)", idx, total, address)
                skipped += 1
        except Exception as exc:
            logger.error("[%d/%d] x Entry #%d failed: %s", idx, total, idx, exc)
            failed += 1

    logger.info("Done — %d inserted, %d skipped, %d failed", inserted, skipped, failed)
