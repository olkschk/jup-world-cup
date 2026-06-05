"""Centralized configuration loaded from environment variables."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# MongoDB
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "jupiterworldcup")
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "wallets")

# Solana
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

# Encryption
FERNET_KEY = os.getenv("FERNET_KEY", "").strip()

# Jupiter
REFERRAL_CODE = os.getenv("REFERRAL_CODE", "").strip()
MIN_SOL_BALANCE = float(os.getenv("MIN_SOL_BALANCE", "0.001"))
JUP_API_BASE = os.getenv("JUP_API_BASE", "https://prediction-market-api.jup.ag/api/v1")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))

# Data files
SEEDS_FILE = DATA_DIR / os.getenv("SEEDS_FILE", "seeds.txt")
PRIVATE_KEYS_FILE = DATA_DIR / os.getenv("PRIVATE_KEYS_FILE", "privatekeys.txt")
PROXIES_FILE = DATA_DIR / os.getenv("PROXIES_FILE", "proxies.txt")

# Concurrency (only used when proxies are present)
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "3"))

# Markets with implied probability >= 70% make the whole match ineligible
MAX_ODDS_PRICE = 700_000


def require_fernet_key() -> str:
    if not FERNET_KEY:
        raise RuntimeError(
            "FERNET_KEY is not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return FERNET_KEY
