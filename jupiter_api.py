"""Jupiter Prediction Market API client.

Flow per wallet:
  1. POST /parlays/referral-code  { "ownerPubkey": address }  → wallet record
  2. POST /parlays/referrals      signed message payload       → referral applied
  3. GET  /events?...             → active markets
  4. Select 5 eligible markets (lower buyYesPriceUsd, skip ≥ 70%)
  5. POST /parlays/free           { ownerPubkey, parlays:[{legMarketIds}] } → paymentTransaction
  6. Sign paymentTransaction with wallet keypair
  7. POST /parlays/free/submit    { ownerPubkey, parlays:[{legMarketIds}], signedTransaction }
"""
from __future__ import annotations

import base64
import logging
import random
import time
from typing import Any

import requests
from requests import HTTPError
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from config import JUP_API_BASE, MAX_ODDS_PRICE, REFERRAL_CODE, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

EVENTS_URL = (
    "https://prediction-market-api.jup.ag/api/v1/events"
    "?start=0&end=100&limit=72"
    "&category=sports&subcategories=fifwc&tags=games"
    "&includeMarkets=true&includeAllMarkets=true"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session(address: str, proxy: str | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": "https://jup.ag",
            "referer": "https://jup.ag/prediction/world-cup",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
        }
    )
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
        logger.debug("Session for %s using proxy %s", address[:8], proxy.split("@")[-1])
    return s


def _url(path: str) -> str:
    return f"{JUP_API_BASE}{path}"


def _raise_with_body(resp: requests.Response) -> None:
    """raise_for_status() but includes the response body in the exception."""
    try:
        resp.raise_for_status()
    except HTTPError as exc:
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:500]
        raise HTTPError(
            f"{exc} | response body: {body}",
            response=resp,
        ) from exc


# ---------------------------------------------------------------------------
# Referral
# ---------------------------------------------------------------------------

def register_referral_code(session: requests.Session, address: str) -> dict[str, Any]:
    """POST /parlays/referral-code → returns wallet record with own referral code."""
    resp = session.post(
        _url("/parlays/referral-code"),
        json={"ownerPubkey": address},
        timeout=REQUEST_TIMEOUT,
    )
    _raise_with_body(resp)
    data = resp.json()
    logger.debug("referral-code response: %s", data)
    return data


def _sign_referral_message(keypair: Keypair, address: str, ref_code: str) -> dict[str, Any]:
    """Build and sign the referral message.

    Message format (plaintext):
        Apply Jupiter prediction-market referral code {ref_code} for wallet {address} at {timestamp_ms}

    The plaintext is base64-encoded for the payload field `message`.
    The ed25519 signature of the raw UTF-8 bytes is base64-encoded for `signature`.
    """
    timestamp_ms = int(time.time() * 1000)
    plaintext = (
        f"Apply Jupiter prediction-market referral code {ref_code} "
        f"for wallet {address} at {timestamp_ms}"
    )
    message_bytes = plaintext.encode("utf-8")
    message_b64 = base64.b64encode(message_bytes).decode()

    sig = keypair.sign_message(message_bytes)
    signature_b64 = base64.b64encode(bytes(sig)).decode()

    return {
        "ownerPubkey": address,
        "referralCode": ref_code,
        "wallet": address,
        "message": message_b64,
        "signature": signature_b64,
        "timestamp": timestamp_ms,
    }


def apply_referral(
    session: requests.Session,
    keypair: Keypair,
    address: str,
) -> dict[str, Any]:
    """Apply REFERRAL_CODE to this wallet.

    Requires a wallet-signed message to authenticate ownership.
    Skipped automatically if REFERRAL_CODE is empty.
    """
    if not REFERRAL_CODE:
        logger.info("REFERRAL_CODE not set — skipping referral step")
        return {}

    payload = _sign_referral_message(keypair, address, REFERRAL_CODE)
    logger.debug("POST /parlays/referrals payload (no sig): ownerPubkey=%s code=%s", address, REFERRAL_CODE)

    resp = session.post(
        _url("/parlays/referrals"),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    _raise_with_body(resp)
    data = resp.json()
    logger.debug("referrals response: %s", data)
    return data


# ---------------------------------------------------------------------------
# Market selection
# ---------------------------------------------------------------------------

def fetch_events() -> list[dict[str, Any]]:
    resp = requests.get(EVENTS_URL, timeout=REQUEST_TIMEOUT)
    _raise_with_body(resp)
    return resp.json().get("data", [])


def _pick_team_market(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the favourite team market for this event, or None if ineligible.

    Challenge rule: if ANY team in the match has buyYesPriceUsd >= MAX_ODDS_PRICE
    (≥ 70% implied probability), the ENTIRE match is ineligible — skip it.

    Among eligible matches, pick the team with the HIGHEST buyYesPriceUsd
    (lowest multiplier = favourite).
    """
    team_markets = [
        m
        for m in event.get("markets", [])
        if m.get("team") is not None and m.get("status") == "open"
    ]
    if len(team_markets) < 2:
        return None

    # If ANY team is at or above the 70% threshold → whole match is ineligible
    for m in team_markets:
        price = (m.get("pricing") or {}).get("buyYesPriceUsd", 0)
        if price >= MAX_ODDS_PRICE:
            return None

    # Pick the favourite: highest implied probability = highest buyYesPriceUsd = lowest multiplier
    team_markets.sort(
        key=lambda m: (m.get("pricing") or {}).get("buyYesPriceUsd", 0),
        reverse=True,
    )
    return team_markets[0]


def select_markets(events: list[dict[str, Any]], count: int = 5) -> list[str]:
    candidates: list[dict[str, Any]] = []
    for event in events:
        market = _pick_team_market(event)
        if market:
            candidates.append(market)

    if len(candidates) < count:
        raise ValueError(
            f"Not enough eligible markets: found {len(candidates)}, need {count}"
        )

    chosen = random.sample(candidates, count)
    market_ids = [m["marketId"] for m in chosen]
    logger.info("Selected markets: %s", market_ids)
    return market_ids


# ---------------------------------------------------------------------------
# Freeroll bet
# ---------------------------------------------------------------------------

def create_free_parlay(
    session: requests.Session,
    address: str,
    leg_market_ids: list[str],
) -> str:
    """POST /parlays/free → returns paymentTransaction (base64 Solana tx)."""
    payload = {
        "ownerPubkey": address,
        "parlays": [{"legMarketIds": leg_market_ids}],
    }
    resp = session.post(
        _url("/parlays/free"),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    _raise_with_body(resp)
    data = resp.json()
    tx_b64 = data.get("paymentTransaction")
    if not tx_b64:
        raise RuntimeError(f"Unexpected /parlays/free response: {data}")
    return tx_b64


def sign_transaction(tx_b64: str, keypair: Keypair) -> str:
    """Deserialize, sign with keypair, return re-serialized base64 tx."""
    raw = base64.b64decode(tx_b64)
    tx = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction(tx.message, [keypair])
    return base64.b64encode(bytes(signed)).decode()


def submit_free_parlay(
    session: requests.Session,
    address: str,
    leg_market_ids: list[str],
    signed_tx_b64: str,
) -> dict[str, Any]:
    """POST /parlays/free/submit → returns txSignature and parlays list.

    signedTransaction goes at the TOP LEVEL (not inside parlays items).
    parlays still contains the legMarketIds array.
    """
    payload = {
        "ownerPubkey": address,
        "parlays": [{"legMarketIds": leg_market_ids}],
        "signedTransaction": signed_tx_b64,
    }
    resp = session.post(
        _url("/parlays/free/submit"),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    _raise_with_body(resp)
    return resp.json()


# ---------------------------------------------------------------------------
# Slip check
# ---------------------------------------------------------------------------

def has_existing_slip(address: str) -> bool:
    resp = requests.get(
        _url(f"/parlays?walletAddress={address}"),
        timeout=REQUEST_TIMEOUT,
    )
    _raise_with_body(resp)
    return bool(resp.json().get("slips", []))
