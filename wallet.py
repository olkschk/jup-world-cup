"""Solana keypair derivation from a BIP39 mnemonic and on-chain balance helpers."""
from __future__ import annotations

import base64
import logging

import base58
import requests
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Changes, Bip44Coins
from solders.keypair import Keypair

from config import REQUEST_TIMEOUT, SOLANA_RPC

DERIVATION_PATH = "m/44'/501'/0'/0'"

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000


def keypair_from_mnemonic(mnemonic: str) -> Keypair:
    """Derive a Solana keypair from a seed phrase using Phantom/Backpack defaults.

    The default derivation path is m/44'/501'/0'/0' which matches what major
    Solana wallets use for the first account. bip-utils handles the
    SLIP-0010 ed25519 derivation internally.
    """
    seed_bytes = Bip39SeedGenerator(mnemonic.strip()).Generate()
    ctx = Bip44.FromSeed(seed_bytes, Bip44Coins.SOLANA)
    # bip-utils' Bip44Coins.SOLANA already encodes m/44'/501'/account'/0';
    # the configurable path lets us override if a different wallet layout is used.
    # SLIP-0010 (ed25519) requires all indexes to be hardened, which
    # bip_utils handles automatically for Bip44Coins.SOLANA.
    # .Change() requires a Bip44Changes enum, not a plain integer.
    derived = ctx.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT)
    priv_bytes = derived.PrivateKey().Raw().ToBytes()
    return Keypair.from_seed(priv_bytes)


def keypair_from_secret_b58(secret_b58: str) -> Keypair:
    return Keypair.from_bytes(base58.b58decode(secret_b58))


def secret_to_b58(keypair: Keypair) -> str:
    return base58.b58encode(bytes(keypair)).decode()


def get_sol_balance(address: str) -> float:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [address, {"commitment": "confirmed"}],
    }
    resp = requests.post(SOLANA_RPC, json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    lamports = data["result"]["value"]
    return lamports / LAMPORTS_PER_SOL
