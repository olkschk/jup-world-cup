"""Main runner.

Sequential mode (no proxies):
    Wallets are processed one by one with random delays between actions.

Parallel mode (proxies present):
    Up to MAX_WORKERS wallets run simultaneously, each through its own proxy
    (assigned round-robin). Random delays are still applied inside each thread.

Re-run behaviour:
    DONE        → skipped
    LOW BALANCE → retried (maybe topped up)
    ERROR       → retried

Usage:
    python main.py
"""
from __future__ import annotations

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from config import (
    MAX_WORKERS,
    MIN_SOL_BALANCE,
    PROXIES_FILE,
    REFERRAL_CODE,
)
from crypto_utils import decrypt
from db import (
    STATUS_DONE,
    STATUS_LOW_BALANCE,
    STATUS_PENDING,
    pending_wallets,
    set_error,
    set_status,
    set_user_ref,
)
from jupiter_api import (
    _session,
    apply_referral,
    create_free_parlay,
    fetch_events,
    has_existing_slip,
    register_referral_code,
    select_markets,
    sign_transaction,
    submit_free_parlay,
)
from wallet import get_sol_balance, keypair_from_secret_b58

logger = logging.getLogger(__name__)

DELAY_MIN = 3
DELAY_MAX = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def rand_sleep(label: str) -> None:
    delay = random.uniform(DELAY_MIN, DELAY_MAX)
    logger.info("  ⏳ %s — waiting %.1f s…", label, delay)
    time.sleep(delay)


def _parse_proxy(raw: str) -> str:
    """Normalise a proxy string to a full URL.

    Supported input formats:
      ip:port                            → http://ip:port
      ip:port:login:password             → http://login:password@ip:port
      http://ip:port                     → unchanged
      http://ip:port:login:password      → http://login:password@ip:port
      http://user:pass@host:port         → unchanged
      socks5://user:pass@host:port       → unchanged
    """
    # Split scheme from the rest (default to http)
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
    else:
        scheme, rest = "http", raw

    # Already in user:pass@host:port form — reconstruct with scheme
    if "@" in rest:
        return f"{scheme}://{rest}"

    parts = rest.split(":")
    if len(parts) == 2:
        # host:port
        return f"{scheme}://{rest}"
    elif len(parts) == 4:
        # host:port:login:password
        host, port, login, password = parts
        return f"{scheme}://{login}:{password}@{host}:{port}"
    else:
        logger.warning("Unrecognised proxy format, using as-is: %s", raw)
        return f"{scheme}://{rest}"


def load_proxies() -> list[str]:
    if not PROXIES_FILE.exists():
        return []
    raw_lines = [l.strip() for l in PROXIES_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    proxies = [_parse_proxy(l) for l in raw_lines]
    logger.debug("Loaded %d proxy/proxies", len(proxies))
    return proxies


# ---------------------------------------------------------------------------
# Per-wallet processing
# ---------------------------------------------------------------------------

def process_wallet(doc: dict, idx: int, total: int, proxy: Optional[str] = None) -> None:
    address: str = doc["address"]
    encrypted_key: str = doc["privatekey"]
    short = address[:8] + "…" + address[-4:]
    prefix = f"[{idx}/{total}][{short}]"
    proxy_tag = f" via {proxy.split('@')[-1]}" if proxy else ""

    logger.info("%s Starting%s", prefix, proxy_tag)

    # ── 1. Balance ───────────────────────────────────────────────────────────
    logger.info("%s Checking SOL balance…", prefix)
    try:
        balance = get_sol_balance(address)
    except Exception as exc:
        set_error(address, f"balance check: {exc}")
        logger.error("%s ❌ Balance check failed: %s", prefix, exc)
        return

    if balance < MIN_SOL_BALANCE:
        set_status(address, STATUS_LOW_BALANCE)
        logger.warning(
            "%s ⚠️  LOW BALANCE: %.6f SOL (min %.4f SOL)", prefix, balance, MIN_SOL_BALANCE
        )
        return

    logger.info("%s ✔  Balance: %.6f SOL", prefix, balance)
    rand_sleep("after balance check")

    # ── 2. Existing slip check ───────────────────────────────────────────────
    logger.info("%s Checking for existing slip…", prefix)
    try:
        if has_existing_slip(address):
            logger.info("%s ✔  Slip already exists — marking DONE", prefix)
            set_status(address, STATUS_DONE, user_ref=True)
            return
        logger.info("%s    No existing slip", prefix)
    except Exception as exc:
        logger.warning("%s ⚠️  Slip check failed: %s — continuing", prefix, exc)
    rand_sleep("after slip check")

    # ── 3. Decrypt keypair ───────────────────────────────────────────────────
    logger.info("%s Decrypting keypair…", prefix)
    try:
        keypair = keypair_from_secret_b58(decrypt(encrypted_key))
        logger.info("%s ✔  Keypair ready", prefix)
    except Exception as exc:
        set_error(address, f"keypair decrypt: {exc}")
        logger.error("%s ❌ Keypair decrypt failed: %s", prefix, exc)
        return

    session = _session(address, proxy)

    # ── 4. Register referral-code record ────────────────────────────────────
    logger.info("%s Registering referral-code on server…", prefix)
    wallet_record: dict = {}
    try:
        wallet_record = register_referral_code(session, address)
        logger.info("%s ✔  Own code: %s", prefix, wallet_record.get("code"))
    except Exception as exc:
        logger.warning("%s ⚠️  referral-code registration: %s (non-fatal)", prefix, exc)
    rand_sleep("after referral-code registration")

    # ── 5. Apply referral ────────────────────────────────────────────────────
    user_ref = False
    if not REFERRAL_CODE:
        logger.info("%s Skipping referral (REFERRAL_CODE not set)", prefix)
    else:
        logger.info("%s Applying referral code %s…", prefix, REFERRAL_CODE)
        try:
            ref_data = apply_referral(session, keypair, address)
            applied = ref_data.get("appliedReferralCode")
            if applied:
                user_ref = True
                set_user_ref(address)
                logger.info("%s ✔  Referral applied: %s — user_ref saved", prefix, applied)
            else:
                logger.info("%s    Referral OK (already applied or empty)", prefix)
        except Exception as exc:
            logger.warning("%s ⚠️  apply_referral: %s — continuing", prefix, exc)
        rand_sleep("after referral")

    # ── 6. Fetch events & select 5 markets ──────────────────────────────────
    logger.info("%s Fetching World Cup markets…", prefix)
    try:
        events = fetch_events()
        logger.info("%s    %d events received", prefix, len(events))
        market_ids = select_markets(events, count=5)
        logger.info("%s ✔  Markets: %s", prefix, ", ".join(market_ids))
    except Exception as exc:
        set_error(address, f"market selection: {exc}")
        logger.error("%s ❌ Market selection failed: %s", prefix, exc)
        return
    rand_sleep("after market selection")

    # ── 7. Create unsigned tx ────────────────────────────────────────────────
    logger.info("%s Creating free parlay transaction…", prefix)
    try:
        tx_b64 = create_free_parlay(session, address, market_ids)
        logger.info("%s ✔  Unsigned transaction received", prefix)
    except Exception as exc:
        set_error(address, f"create_free_parlay: {exc}")
        logger.error("%s ❌ create_free_parlay failed: %s", prefix, exc)
        return
    rand_sleep("after tx creation")

    # ── 8. Sign ─────────────────────────────────────────────────────────────
    logger.info("%s Signing transaction…", prefix)
    try:
        signed_tx = sign_transaction(tx_b64, keypair)
        logger.info("%s ✔  Transaction signed", prefix)
    except Exception as exc:
        set_error(address, f"sign_transaction: {exc}")
        logger.error("%s ❌ Signing failed: %s", prefix, exc)
        return
    rand_sleep("after signing")

    # ── 9. Submit ────────────────────────────────────────────────────────────
    logger.info("%s Submitting freeroll…", prefix)
    try:
        result = submit_free_parlay(session, address, market_ids, signed_tx)
        parlay_id = result.get("parlays", [{}])[0].get("parlayId", "?")
        logger.info("%s ✅ Freeroll submitted! parlayId=%s", prefix, parlay_id)
        set_status(address, STATUS_DONE, user_ref=user_ref if user_ref else None)
        logger.info("%s ✔  Status → DONE", prefix)
    except Exception as exc:
        set_error(address, f"submit_free_parlay: {exc}")
        logger.error("%s ❌ Submit failed: %s", prefix, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    proxies = load_proxies()
    wallets = list(pending_wallets())

    if not wallets:
        logger.info("No pending wallets. All done.")
        return

    total = len(wallets)
    mode = f"parallel (MAX_WORKERS={MAX_WORKERS})" if proxies else "sequential"

    logger.info("=" * 60)
    logger.info("Wallets: %d | Mode: %s | Proxies: %d", total, mode, len(proxies))
    logger.info("Delay per action: %d–%d s", DELAY_MIN, DELAY_MAX)
    logger.info("=" * 60)

    if proxies:
        # Assign proxy round-robin; each thread runs one wallet
        tasks = [
            (doc, idx + 1, total, proxies[idx % len(proxies)])
            for idx, doc in enumerate(wallets)
        ]

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(process_wallet, doc, idx, total, proxy): doc["address"]
                for doc, idx, total, proxy in tasks
            }
            for future in as_completed(futures):
                address = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.error("Unhandled error for %s: %s", address, exc)
    else:
        # Sequential — no proxies
        for idx, doc in enumerate(wallets, 1):
            address = doc["address"]
            status = doc.get("status", STATUS_PENDING)
            logger.info("")
            logger.info("── Wallet %d/%d ──────────────────────────────────────", idx, total)
            logger.info("   Address : %s", address)
            logger.info("   Status  : %s", status)

            process_wallet(doc, idx, total, proxy=None)

            if idx < total:
                rand_sleep(f"between wallets ({idx}/{total} done)")

    logger.info("")
    logger.info("=" * 60)
    logger.info("Run complete — %d wallet(s) processed", total)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
