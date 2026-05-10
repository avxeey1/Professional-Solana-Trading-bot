"""
config.py — Centralised configuration & constants
"""
from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_OWNER_ID: int = int(os.environ["TELEGRAM_OWNER_ID"])

# ── Solana RPC ────────────────────────────────────────────────────────────────
SOLANA_RPC_URL: str = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SOLANA_WS_URL: str  = os.getenv("SOLANA_WS_URL",  "wss://api.mainnet-beta.solana.com")
JUPITER_API_URL: str = os.getenv("JUPITER_API_URL", "https://quote-api.jup.ag/v6")

# ── Security ──────────────────────────────────────────────────────────────────
ENCRYPTION_KEY: str = os.environ["ENCRYPTION_KEY"]       # 32-byte hex

# ── APIs (optional but recommended) ──────────────────────────────────────────
HELIUS_API_KEY: str = os.getenv("HELIUS_API_KEY", "")
BIRDEYE_API_KEY: str = os.getenv("BIRDEYE_API_KEY", "")

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data/bot.db")
Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)

# ── Well-known program IDs ────────────────────────────────────────────────────
WSOL_MINT          = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM_ID   = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_ID      = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
RAYDIUM_AMM_V4     = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM       = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
PUMP_FUN_PROGRAM   = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_FUN_MIGRATION = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
METEORA_DLMM       = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"

# ── Default trading parameters ────────────────────────────────────────────────
DEFAULT_SLIPPAGE_BPS   = 500       # 5 %
DEFAULT_POSITION_PCT   = 5.0       # 5 % of wallet balance per trade
DEFAULT_TAKE_PROFIT    = 2.0       # 2× (100 % gain)
DEFAULT_TRAILING_STOP  = 0.0       # disabled
DEFAULT_COOLDOWN_SEC   = 30
DEFAULT_MAX_DAILY_TRADES = 20
DEFAULT_DAILY_LOSS_CAP   = 2.0     # SOL
DEFAULT_MIN_LIQUIDITY_USD = 5_000
DEFAULT_MIN_TOKEN_AGE_H   = 0.083  # 5 minutes
DEFAULT_TRADING_WINDOW_START = "00:00"
DEFAULT_TRADING_WINDOW_END   = "23:59"

# ── Scanner ───────────────────────────────────────────────────────────────────
SCANNER_INTERVAL_SEC       = 15    # how often to poll for new tokens
SCANNER_MAX_TOKEN_AGE_MIN  = 30    # ignore tokens older than this
PRICE_MONITOR_INTERVAL_SEC = 8     # open-position price-check interval
ALERT_MONITOR_INTERVAL_SEC = 20

# ── Misc ──────────────────────────────────────────────────────────────────────
LOG_LINES_TAIL   = 20
HISTORY_DEFAULT  = 10
VERSION          = "2.0.0"
