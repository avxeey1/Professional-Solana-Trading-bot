"""
core/database.py — Async SQLite persistence layer
All tables are created on first run.  Never store raw private keys —
only AES-encrypted blobs (see utils/crypto.py).
"""
from __future__ import annotations
import json
import time
import aiosqlite
from typing import Any, Optional
from config import DATABASE_PATH


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────
_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS bot_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wallets (
    label       TEXT PRIMARY KEY,
    pubkey      TEXT NOT NULL UNIQUE,
    enc_privkey TEXT NOT NULL,          -- AES-256-GCM encrypted
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS signal_channels (
    channel_id   TEXT PRIMARY KEY,
    label        TEXT,
    added_at     REAL NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS blacklist (
    mint TEXT PRIMARY KEY,
    reason TEXT,
    added_at REAL NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS whitelist (
    mint TEXT PRIMARY KEY,
    added_at REAL NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS open_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_label    TEXT NOT NULL,
    mint            TEXT NOT NULL,
    symbol          TEXT,
    entry_price_sol REAL NOT NULL,
    token_amount    REAL NOT NULL,
    sol_spent       REAL NOT NULL,
    take_profit_x   REAL NOT NULL,
    trailing_stop   REAL NOT NULL DEFAULT 0,
    peak_price_sol  REAL,
    buy_tx          TEXT,
    opened_at       REAL NOT NULL DEFAULT (unixepoch('now')),
    paper           INTEGER NOT NULL DEFAULT 0,
    source          TEXT DEFAULT 'signal',   -- signal | scanner | manual
    UNIQUE(wallet_label, mint)
);

CREATE TABLE IF NOT EXISTS trade_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_label    TEXT NOT NULL,
    mint            TEXT NOT NULL,
    symbol          TEXT,
    entry_price_sol REAL,
    exit_price_sol  REAL,
    token_amount    REAL,
    sol_spent       REAL,
    sol_received    REAL,
    pnl_sol         REAL,
    pnl_pct         REAL,
    buy_tx          TEXT,
    sell_tx         TEXT,
    opened_at       REAL,
    closed_at       REAL NOT NULL DEFAULT (unixepoch('now')),
    paper           INTEGER NOT NULL DEFAULT 0,
    reason          TEXT,                    -- take_profit | trailing_stop | manual | kill
    source          TEXT DEFAULT 'signal'
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL DEFAULT (unixepoch('now')),
    level      TEXT NOT NULL DEFAULT 'INFO',
    message    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mint        TEXT NOT NULL,
    target_usd  REAL NOT NULL,
    created_at  REAL NOT NULL DEFAULT (unixepoch('now')),
    triggered   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date        TEXT PRIMARY KEY,           -- YYYY-MM-DD
    trades      INTEGER NOT NULL DEFAULT 0,
    wins        INTEGER NOT NULL DEFAULT 0,
    losses      INTEGER NOT NULL DEFAULT 0,
    pnl_sol     REAL NOT NULL DEFAULT 0
);
"""


# ─────────────────────────────────────────────────────────────────────────────
# Connection helper
# ─────────────────────────────────────────────────────────────────────────────
async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(_SCHEMA)
    await db.commit()
    return db


# ─────────────────────────────────────────────────────────────────────────────
# Settings helpers
# ─────────────────────────────────────────────────────────────────────────────
async def get_setting(key: str, default: Any = None) -> Any:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT value FROM bot_settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


async def set_setting(key: str, value: Any) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO bot_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Audit log
# ─────────────────────────────────────────────────────────────────────────────
async def audit(message: str, level: str = "INFO") -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO audit_log(ts,level,message) VALUES(?,?,?)",
            (time.time(), level, message),
        )
        await db.commit()


async def get_audit_tail(n: int = 20) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT ts,level,message FROM audit_log ORDER BY id DESC LIMIT ?", (n,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in reversed(rows)]


# ─────────────────────────────────────────────────────────────────────────────
# Wallets
# ─────────────────────────────────────────────────────────────────────────────
async def add_wallet(label: str, pubkey: str, enc_privkey: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO wallets(label,pubkey,enc_privkey) VALUES(?,?,?)",
            (label, pubkey, enc_privkey),
        )
        await db.commit()


async def get_wallet(label: str) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM wallets WHERE label=?", (label,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_all_wallets() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM wallets ORDER BY created_at") as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def toggle_wallet(label: str) -> bool:
    """Returns new enabled state."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute("SELECT enabled FROM wallets WHERE label=?", (label,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise ValueError(f"Wallet '{label}' not found")
        new_state = 0 if row[0] else 1
        await db.execute("UPDATE wallets SET enabled=? WHERE label=?", (new_state, label))
        await db.commit()
    return bool(new_state)


# ─────────────────────────────────────────────────────────────────────────────
# Signal channels
# ─────────────────────────────────────────────────────────────────────────────
async def add_channel(channel_id: str, label: str = "") -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO signal_channels(channel_id,label) VALUES(?,?)",
            (channel_id, label),
        )
        await db.commit()


async def remove_channel(channel_id: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM signal_channels WHERE channel_id=?", (channel_id,))
        await db.commit()


async def get_channels() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM signal_channels ORDER BY added_at") as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Black / white list
# ─────────────────────────────────────────────────────────────────────────────
async def blacklist_add(mint: str, reason: str = "") -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO blacklist(mint,reason) VALUES(?,?)", (mint, reason)
        )
        await db.commit()


async def blacklist_remove(mint: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM blacklist WHERE mint=?", (mint,))
        await db.commit()


async def is_blacklisted(mint: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute("SELECT 1 FROM blacklist WHERE mint=?", (mint,)) as cur:
            return await cur.fetchone() is not None


async def whitelist_add(mint: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO whitelist(mint) VALUES(?)", (mint,))
        await db.commit()


async def whitelist_remove(mint: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM whitelist WHERE mint=?", (mint,))
        await db.commit()


async def is_whitelisted(mint: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute("SELECT 1 FROM whitelist WHERE mint=?", (mint,)) as cur:
            return await cur.fetchone() is not None


async def get_blacklist() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT mint,reason,added_at FROM blacklist ORDER BY added_at DESC") as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_whitelist() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT mint,added_at FROM whitelist ORDER BY added_at") as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Positions
# ─────────────────────────────────────────────────────────────────────────────
async def open_position(
    wallet_label: str, mint: str, symbol: str, entry_price_sol: float,
    token_amount: float, sol_spent: float, take_profit_x: float,
    trailing_stop: float = 0.0, buy_tx: str = "", paper: bool = False,
    source: str = "signal",
) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO open_positions
               (wallet_label,mint,symbol,entry_price_sol,token_amount,
                sol_spent,take_profit_x,trailing_stop,peak_price_sol,buy_tx,paper,source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (wallet_label, mint, symbol, entry_price_sol, token_amount,
             sol_spent, take_profit_x, trailing_stop, entry_price_sol,
             buy_tx, int(paper), source),
        )
        await db.commit()


async def update_peak_price(wallet_label: str, mint: str, peak: float) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE open_positions SET peak_price_sol=? WHERE wallet_label=? AND mint=?",
            (peak, wallet_label, mint),
        )
        await db.commit()


async def get_open_positions() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM open_positions ORDER BY opened_at") as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_position(wallet_label: str, mint: str) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM open_positions WHERE wallet_label=? AND mint=?",
            (wallet_label, mint),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def close_position(wallet_label: str, mint: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "DELETE FROM open_positions WHERE wallet_label=? AND mint=?",
            (wallet_label, mint),
        )
        await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Trade history
# ─────────────────────────────────────────────────────────────────────────────
async def record_trade(
    wallet_label: str, mint: str, symbol: str,
    entry_price_sol: float, exit_price_sol: float,
    token_amount: float, sol_spent: float, sol_received: float,
    buy_tx: str, sell_tx: str, opened_at: float,
    paper: bool = False, reason: str = "take_profit", source: str = "signal",
) -> None:
    pnl_sol = sol_received - sol_spent
    pnl_pct  = (pnl_sol / sol_spent * 100) if sol_spent else 0.0
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO trade_history
               (wallet_label,mint,symbol,entry_price_sol,exit_price_sol,
                token_amount,sol_spent,sol_received,pnl_sol,pnl_pct,
                buy_tx,sell_tx,opened_at,paper,reason,source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (wallet_label, mint, symbol, entry_price_sol, exit_price_sol,
             token_amount, sol_spent, sol_received, pnl_sol, pnl_pct,
             buy_tx, sell_tx, opened_at, int(paper), reason, source),
        )
        await db.commit()
    # update daily stats
    import datetime
    date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    win = 1 if pnl_sol >= 0 else 0
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO daily_stats(date,trades,wins,losses,pnl_sol)
               VALUES(?,1,?,?,?)
               ON CONFLICT(date) DO UPDATE SET
                 trades=trades+1,
                 wins=wins+?,
                 losses=losses+?,
                 pnl_sol=pnl_sol+?""",
            (date_str, win, 1 - win, pnl_sol, win, 1 - win, pnl_sol),
        )
        await db.commit()


async def get_history(n: int = 10, paper_only: bool = False) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        filt = "WHERE paper=1" if paper_only else ""
        async with db.execute(
            f"SELECT * FROM trade_history {filt} ORDER BY closed_at DESC LIMIT ?", (n,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_pnl_summary() -> dict:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT wallet_label,
                      COUNT(*) trades,
                      SUM(CASE WHEN pnl_sol>0 THEN 1 ELSE 0 END) wins,
                      SUM(CASE WHEN pnl_sol<=0 THEN 1 ELSE 0 END) losses,
                      SUM(pnl_sol) total_pnl,
                      MAX(pnl_sol) best,
                      MIN(pnl_sol) worst
               FROM trade_history WHERE paper=0
               GROUP BY wallet_label"""
        ) as cur:
            rows = await cur.fetchall()
    return {r["wallet_label"]: dict(r) for r in rows}


async def get_daily_report(date_str: str) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM daily_stats WHERE date=?", (date_str,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_top_tokens(n: int = 10) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT mint, symbol, COUNT(*) trades, SUM(pnl_sol) total_pnl
               FROM trade_history WHERE paper=0
               GROUP BY mint ORDER BY total_pnl DESC LIMIT ?""",
            (n,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def clear_history() -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM trade_history")
        await db.execute("DELETE FROM daily_stats")
        await db.commit()


async def get_today_stats() -> dict:
    import datetime
    date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    result = await get_daily_report(date_str)
    return result or {"date": date_str, "trades": 0, "wins": 0, "losses": 0, "pnl_sol": 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# Price Alerts
# ─────────────────────────────────────────────────────────────────────────────
async def add_alert(mint: str, target_usd: float) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "INSERT INTO price_alerts(mint,target_usd) VALUES(?,?)", (mint, target_usd)
        )
        await db.commit()
        return cur.lastrowid


async def get_alerts(triggered: bool = False) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM price_alerts WHERE triggered=?", (int(triggered),)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_alert_triggered(alert_id: int) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE price_alerts SET triggered=1 WHERE id=?", (alert_id,))
        await db.commit()


async def remove_alert(mint: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM price_alerts WHERE mint=? AND triggered=0", (mint,))
        await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Streak helper
# ─────────────────────────────────────────────────────────────────────────────
async def get_streak() -> tuple[str, int]:
    """Returns (type, count) where type is 'win' or 'loss'."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT pnl_sol FROM trade_history WHERE paper=0 ORDER BY closed_at DESC LIMIT 50"
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return ("none", 0)
    streak_type = "win" if rows[0]["pnl_sol"] >= 0 else "loss"
    count = 0
    for r in rows:
        is_win = r["pnl_sol"] >= 0
        if (streak_type == "win" and is_win) or (streak_type == "loss" and not is_win):
            count += 1
        else:
            break
    return (streak_type, count)


# ─────────────────────────────────────────────────────────────────────────────
# Heatmap (best hours)
# ─────────────────────────────────────────────────────────────────────────────
async def get_heatmap() -> dict[int, dict]:
    """Returns {hour: {trades, pnl_sol}}"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT CAST(strftime('%H', datetime(closed_at,'unixepoch')) AS INT) hour,
                      COUNT(*) trades, SUM(pnl_sol) pnl_sol
               FROM trade_history WHERE paper=0
               GROUP BY hour ORDER BY hour"""
        ) as cur:
            rows = await cur.fetchall()
    return {r["hour"]: {"trades": r["trades"], "pnl_sol": r["pnl_sol"]} for r in rows}
