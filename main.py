"""Jupiter World Cup Freeroll Bot

Usage:
    python main.py init     — load wallets from data/seeds.txt or data/privatekeys.txt
    python main.py run      — place freeroll bets for all pending wallets
    python main.py stats    — show statistics and low-balance wallets
    python main.py results  — show per-wallet bet results (win/loss/pending)
"""
from __future__ import annotations

import logging
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from config import (
    MAX_WORKERS,
    MIN_SOL_BALANCE,
    PROXIES_FILE,
    REFERRAL_CODE,
)
from requests.exceptions import ProxyError
from crypto_utils import decrypt
from db import (
    STATUS_DONE,
    STATUS_LOW_BALANCE,
    STATUS_PENDING,
    cache_event_score,
    cache_slip,
    done_wallets,
    get_cached_score,
    get_cached_slip,
    get_stats,
    init_db,
    init_results_tables,
    pending_wallets,
    set_error,
    set_is_lost,
    set_status,
    set_user_ref,
)
from jupiter_api import (
    _session,
    apply_referral,
    create_free_parlay,
    fetch_all_events_with_markets,
    fetch_event_score,
    fetch_events,
    fetch_wallet_slip,
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
    """Normalise any proxy format to a full URL.

    Supported:
      ip:port                       → http://ip:port
      ip:port:login:password        → http://login:password@ip:port
      http://ip:port                → unchanged
      http://ip:port:login:password → http://login:password@ip:port
      http://user:pass@host:port    → unchanged
      socks5://...                  → unchanged
    """
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
    else:
        scheme, rest = "http", raw

    if "@" in rest:
        return f"{scheme}://{rest}"

    parts = rest.split(":")
    if len(parts) == 4:
        host, port, login, password = parts
        return f"{scheme}://{login}:{password}@{host}:{port}"
    elif len(parts) == 2:
        return f"{scheme}://{rest}"
    else:
        logger.warning("Unrecognised proxy format, using as-is: %s", raw)
        return f"{scheme}://{rest}"


def load_proxies() -> list[str]:
    if not PROXIES_FILE.exists():
        return []
    raw = [l.strip() for l in PROXIES_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [_parse_proxy(l) for l in raw]


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

    # ── 1. Balance ──────────────────────────────────────────────────────────
    logger.info("%s Checking SOL balance…", prefix)
    try:
        balance = get_sol_balance(address)
    except Exception as exc:
        set_error(address, f"balance check: {exc}")
        logger.error("%s ❌ Balance check failed: %s", prefix, exc)
        return

    if balance < MIN_SOL_BALANCE:
        set_status(address, STATUS_LOW_BALANCE)
        logger.warning("%s ⚠️  LOW BALANCE: %.6f SOL (min %.4f)", prefix, balance, MIN_SOL_BALANCE)
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

    # ── 4. Register referral-code ────────────────────────────────────────────
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

    # ── 6. Markets ───────────────────────────────────────────────────────────
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
        logger.info("%s ✔  Unsigned tx received", prefix)
    except Exception as exc:
        set_error(address, f"create_free_parlay: {exc}")
        logger.error("%s ❌ create_free_parlay failed: %s", prefix, exc)
        return

    # ── 8. Sign ─────────────────────────────────────────────────────────────
    # No sleep between create/sign/submit — transaction validity window is short.
    logger.info("%s Signing transaction…", prefix)
    try:
        signed_tx = sign_transaction(tx_b64, keypair)
        logger.info("%s ✔  Transaction signed", prefix)
    except Exception as exc:
        set_error(address, f"sign_transaction: {exc}")
        logger.error("%s ❌ Signing failed: %s", prefix, exc)
        return

    # ── 9. Submit ────────────────────────────────────────────────────────────
    logger.info("%s Submitting freeroll…", prefix)
    try:
        result = submit_free_parlay(session, address, market_ids, signed_tx)
        parlay_id = result.get("parlays", [{}])[0].get("parlayId", "?")
        logger.info("%s ✅ Submitted! parlayId=%s", prefix, parlay_id)
        set_status(address, STATUS_DONE, user_ref=user_ref if user_ref else None)
        logger.info("%s ✔  Status → DONE", prefix)
    except Exception as exc:
        set_error(address, f"submit_free_parlay: {exc}")
        logger.error("%s ❌ Submit failed: %s", prefix, exc)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init() -> None:
    from init_wallets import run_init
    run_init()


def cmd_run() -> None:
    init_db()
    proxies = load_proxies()
    wallets = list(pending_wallets())

    if not wallets:
        logger.info("No pending wallets. All done.")
        return

    total = len(wallets)
    mode = f"parallel MAX_WORKERS={MAX_WORKERS}" if proxies else "sequential"
    logger.info("=" * 60)
    logger.info("Wallets: %d | Mode: %s | Proxies: %d", total, mode, len(proxies))
    logger.info("Delay per action: %d–%d s", DELAY_MIN, DELAY_MAX)
    logger.info("=" * 60)

    if proxies:
        tasks = [
            (doc, idx + 1, total, proxies[idx % len(proxies)])
            for idx, doc in enumerate(wallets)
        ]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(process_wallet, doc, idx, tot, proxy): doc["address"]
                for doc, idx, tot, proxy in tasks
            }
            for future in as_completed(futures):
                addr = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.error("Unhandled error for %s: %s", addr, exc)
    else:
        for idx, doc in enumerate(wallets, 1):
            logger.info("")
            logger.info("── Wallet %d/%d: %s", idx, total, doc["address"])
            process_wallet(doc, idx, total, proxy=None)
            if idx < total:
                rand_sleep(f"between wallets ({idx}/{total} done)")

    logger.info("")
    logger.info("=" * 60)
    logger.info("Run complete — %d wallet(s) processed", total)
    logger.info("=" * 60)


def cmd_stats() -> None:
    init_db()
    s = get_stats()
    total = s["total"]

    def pct(n: int) -> str:
        return f"{n / total * 100:.1f}%" if total else "—"

    sep = "=" * 60
    print(sep)
    print("  STATISTICS")
    print(sep)
    print(f"  Total wallets : {total}")
    print()

    order = [STATUS_DONE, STATUS_PENDING, STATUS_LOW_BALANCE]
    shown = set()
    for status in order:
        cnt = s["by_status"].get(status, 0)
        print(f"  {status:<20} {cnt:>5}  ({pct(cnt)})")
        shown.add(status)
    # any ERROR or other statuses
    for status, cnt in s["by_status"].items():
        if status not in shown:
            print(f"  {status[:20]:<20} {cnt:>5}  ({pct(cnt)})")

    print()
    print(f"  Referral applied : {s['user_ref_count']:>5}  ({pct(s['user_ref_count'])})")
    print(sep)

    lb = s["low_balance_wallets"]
    if lb:
        print(f"\n  LOW BALANCE wallets ({len(lb)}):")
        print(f"  {'Address':<46}  Balance")
        print("  " + "-" * 54)
        for addr in lb:
            try:
                bal = get_sol_balance(addr)
                bal_str = f"{bal:.6f} SOL"
            except Exception:
                bal_str = "— (RPC error)"
            print(f"  {addr:<46}  {bal_str}")
    else:
        print("\n  No LOW BALANCE wallets.")

    errs = s["error_wallets"]
    if errs:
        print(f"\n  ERROR wallets ({len(errs)}):")
        print("  " + "-" * 54)
        for addr, status in errs:
            short_status = status[7:60] if status.startswith("ERROR:") else status
            print(f"  {addr}  {short_status}")

    print()


def cmd_results() -> None:
    init_db()
    init_results_tables()
    proxies = load_proxies()
    wallets = done_wallets()

    if not wallets:
        logger.info("No DONE wallets.")
        print("No DONE wallets found.")
        return

    # Fetch all events (incl. closed) once — build market_id → result lookup
    proxy0 = proxies[0] if proxies else None
    logger.info("Fetching all events with market results…")
    try:
        all_events = fetch_all_events_with_markets(proxy0)
    except Exception as exc:
        logger.error("Failed to fetch events: %s", exc)
        print(f"ERROR: could not fetch events: {exc}")
        return

    # market_id → {result, homeTeam, awayTeam, team}
    # Use None (not "?") for missing team names so the `or` fallback to
    # score_data works correctly in the display loop below.
    market_lookup: dict[str, dict] = {}
    for ev in all_events:
        home = ev.get("homeTeam")
        away = ev.get("awayTeam")
        for mkt in ev.get("markets", []):
            mid = mkt.get("marketId")
            if mid:
                team_val = mkt.get("team")
                team_name = (
                    team_val.get("name") if isinstance(team_val, dict) else team_val
                )
                market_lookup[mid] = {
                    "result": mkt.get("result"),
                    "homeTeam": home,
                    "awayTeam": away,
                    "team": team_name,
                }
    logger.info(
        "Market lookup: %d markets across %d events", len(market_lookup), len(all_events)
    )

    total = len(wallets)
    sep = "=" * 70
    print(sep)
    print("  RESULTS")
    print(sep)

    tally_won = tally_lost = tally_pending = 0
    leg_wins = leg_losses = leg_pending = 0

    for idx, doc in enumerate(wallets, 1):
        address: str = doc["address"]
        proxy = proxies[(idx - 1) % len(proxies)] if proxies else None
        short = address[:8] + "…" + address[-4:]

        # Slip — prefer cache, fall back to API
        legs = get_cached_slip(address)
        if legs is None:
            for attempt_proxy in ([proxy, None] if proxy else [None]):
                try:
                    parlay_id, legs = fetch_wallet_slip(address, attempt_proxy)
                    if legs:
                        cache_slip(address, parlay_id, legs)
                    break
                except ProxyError:
                    if attempt_proxy is None:
                        logger.warning("Slip fetch failed without proxy for %s", short)
                        legs = []
                    else:
                        logger.warning("Proxy 407 for %s, retrying without proxy", short)
                except Exception as exc:
                    logger.warning("Slip fetch failed for %s: %s", short, exc)
                    legs = []
                    break
            if legs is None:
                legs = []

        if not legs:
            print(f"\n  [{idx}/{total}] {short}  — no slip found")
            continue

        leg_rows: list[dict] = []
        for leg in legs:
            event_id: str = leg["eventId"]
            market_id: str = leg["marketId"]

            # Score — skip re-fetch if already cached as ended
            cached_score = get_cached_score(event_id)
            if cached_score and cached_score["ended"]:
                score_data = cached_score
            else:
                score_data = cached_score  # fallback if all fetches fail
                for attempt_proxy in ([proxy, None] if proxy else [None]):
                    try:
                        raw = fetch_event_score(event_id, attempt_proxy)
                        if raw is not None:
                            cache_event_score(event_id, raw)
                            score_data = raw
                        else:
                            score_data = None
                        break
                    except ProxyError:
                        if attempt_proxy is None:
                            logger.warning("Score fetch failed without proxy %s", event_id)
                        else:
                            logger.debug("Proxy 407 for score %s, retrying without proxy", event_id)
                    except Exception as exc:
                        logger.warning("Score fetch failed %s: %s", event_id, exc)
                        break

            mkt_info = market_lookup.get(market_id, {})
            result = mkt_info.get("result")

            # score_data can come from fresh API (camelCase) or SQLite cache (snake_case)
            sd = score_data or {}
            home = (
                sd.get("homeTeam") or sd.get("home_team")
                or mkt_info.get("homeTeam")
                or mkt_info.get("team")
                or "?"
            )
            away = (
                sd.get("awayTeam") or sd.get("away_team")
                or mkt_info.get("awayTeam")
                or "?"
            )

            if score_data:
                score_str = score_data.get("score") or "—"
                status_str = score_data.get("status") or ""
            else:
                score_str = "—"
                status_str = "Not played yet"

            icon = "✅" if result == "yes" else ("❌" if result == "no" else "⏳")
            leg_rows.append(
                {
                    "icon": icon,
                    "home": home,
                    "away": away,
                    "score": score_str,
                    "status": status_str,
                    "result": result,
                }
            )

        wins = sum(1 for r in leg_rows if r["result"] == "yes")
        losses = sum(1 for r in leg_rows if r["result"] == "no")
        pending = sum(1 for r in leg_rows if r["result"] is None)

        leg_wins += wins
        leg_losses += losses
        leg_pending += pending

        if losses:
            tally_lost += 1
            set_is_lost(address)
            continue  # skip display — lost wallets are filtered out

        if pending:
            parlay_icon = "⏳"
            tally_pending += 1
        else:
            parlay_icon = "✅"
            tally_won += 1

        print(f"\n  [{idx}/{total}] {short}  {parlay_icon}  ({wins}W / {pending}⏳)")
        for r in leg_rows:
            print(f"    {r['icon']}  {r['home']} vs {r['away']}  {r['score']}  {r['status']}")

    total_legs = leg_wins + leg_losses + leg_pending
    print(f"\n{sep}")
    print(f"  Parlay summary ({total} wallets):")
    print(f"    ✅ All 5 won   = {tally_won:<4}  (parlay fully resolved as win)")
    print(f"    ❌ Parlay lost = {tally_lost:<4}  (1+ leg lost → is_lost marked in DB)")
    print(f"    ⏳ Still alive = {tally_pending:<4}  (no losses yet, awaiting results)")
    print(f"  Individual legs ({total_legs} total):  ✅ {leg_wins}W  ❌ {leg_losses}L  ⏳ {leg_pending}P")
    print(sep)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMANDS = {
    "init": cmd_init,
    "run": cmd_run,
    "stats": cmd_stats,
    "results": cmd_results,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print(f"Available commands: {', '.join(COMMANDS)}")
        sys.exit(1)

    COMMANDS[sys.argv[1]]()
