"""One-time initialisation script.

Priority:
  1. data/seeds.txt  — if non-empty, derive keypairs via BIP44 from seed phrases
  2. data/privatekeys.txt — fallback if seeds.txt is empty or missing

Usage:
    python init_wallets.py
"""
from __future__ import annotations

import logging
import sys

from config import PRIVATE_KEYS_FILE, SEEDS_FILE
from crypto_utils import encrypt
from db import upsert_wallet
from wallet import keypair_from_mnemonic, keypair_from_secret_b58, secret_to_b58

logger = logging.getLogger(__name__)


def _read_lines(path) -> list[str]:
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> None:
    seeds = _read_lines(SEEDS_FILE)
    private_keys = _read_lines(PRIVATE_KEYS_FILE)

    if seeds:
        source = "seeds"
        entries = seeds
        logger.info("Using seeds.txt (%d entries)", len(entries))
    elif private_keys:
        source = "privatekeys"
        entries = private_keys
        logger.info("seeds.txt empty — using privatekeys.txt (%d entries)", len(entries))
    else:
        logger.error("Both seeds.txt and privatekeys.txt are empty or missing in data/")
        sys.exit(1)

    total = len(entries)
    ok, fail = 0, 0

    for idx, entry in enumerate(entries, 1):
        try:
            if source == "seeds":
                kp = keypair_from_mnemonic(entry)
            else:
                kp = keypair_from_secret_b58(entry)

            address = str(kp.pubkey())
            encrypted_key = encrypt(secret_to_b58(kp))
            upsert_wallet(address, encrypted_key)
            logger.info("[%d/%d] ✔  %s", idx, total, address)
            ok += 1
        except Exception as exc:
            logger.error("[%d/%d] ❌ Entry #%d failed: %s", idx, total, idx, exc)
            fail += 1

    logger.info("Done — %d OK, %d failed", ok, fail)


if __name__ == "__main__":
    main()
