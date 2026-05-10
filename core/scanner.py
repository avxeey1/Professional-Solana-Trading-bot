"""
core/scanner.py — New token scanner.
Monitors pump.fun and Raydium new-pool events for degen opportunities.
Applies safety checks before triggering trades.
"""
from __future__ import annotations
import asyncio
import time
from typing import Optional

import aiohttp

import config
from core.database import audit, get_setting
from core.trader import full_buy_flow
from utils.state import BotState


# ─────────────────────────────────────────────────────────────────────────────
# Pump.fun new token feed
# ─────────────────────────────────────────────────────────────────────────────

PUMP_FUN_API = "https://frontend-api.pump.fun/coins"
_seen_tokens: set[str] = set()


async def fetch_new_pump_tokens() -> list[dict]:
    """
    Poll pump.fun API for recently created tokens.
    Returns list of {mint, symbol, name, created_timestamp, market_cap}
    """
    try:
        params = {
            "offset": 0,
            "limit": 50,
            "sort": "created_timestamp",
            "order": "DESC",
            "includeNsfw": "false",
        }
        async with aiohttp.ClientSession() as s:
            async with s.get(
                PUMP_FUN_API,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "Mozilla/5.0"},
            ) as r:
                data = await r.json()
        if not isinstance(data, list):
            return []
        return [
            {
                "mint": t.get("mint", ""),
                "symbol": t.get("symbol", ""),
                "name": t.get("name", ""),
                "created_timestamp": t.get("created_timestamp", 0),
                "market_cap": t.get("market_cap", 0),
                "reply_count": t.get("reply_count", 0),
                "source": "pump.fun",
            }
            for t in data
            if t.get("mint")
        ]
    except Exception as e:
        await audit(f"Pump.fun scanner error: {e}", "ERROR")
        return []


async def fetch_new_raydium_pools() -> list[dict]:
    """
    Fetch recently added Raydium pools via the Raydium API.
    Returns list of {mint, symbol, source}
    """
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.raydium.io/v2/main/pairs",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json()

        if not isinstance(data, list):
            return []

        now = time.time()
        recent = []
        for pool in data:
            # Keep only pools created in last 30 minutes
            lp_minted = pool.get("lpMint")
            base_mint = pool.get("baseMint")
            if not base_mint or base_mint == config.WSOL_MINT:
                continue
            # No creation timestamp in this API, filter by SOL pair
            quote = pool.get("quoteMint", "")
            if quote != config.WSOL_MINT:
                continue
            recent.append({
                "mint": base_mint,
                "symbol": pool.get("name", "").split("-")[0].strip(),
                "name": pool.get("name", ""),
                "created_timestamp": now,
                "market_cap": 0,
                "source": "raydium",
            })
        return recent[:20]  # cap to 20
    except Exception as e:
        await audit(f"Raydium scanner error: {e}", "ERROR")
        return []


async def fetch_meteora_new_pools() -> list[dict]:
    """
    Fetch new Meteora DLMM pools for meme coins.
    """
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://dlmm-api.meteora.ag/pair/all_with_pagination",
                params={"page": 0, "limit": 20, "sort_key": "created_at", "order_by": "desc"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json()
        pairs = data.get("pairs", []) if isinstance(data, dict) else []
        result = []
        for p in pairs:
            base_mint = p.get("mint_x") or p.get("mint_y")
            if not base_mint or base_mint == config.WSOL_MINT:
                continue
            if p.get("mint_x") == config.WSOL_MINT:
                base_mint = p.get("mint_y")
            elif p.get("mint_y") == config.WSOL_MINT:
                base_mint = p.get("mint_x")
            result.append({
                "mint": base_mint,
                "symbol": p.get("name", "???").split("-")[0],
                "name": p.get("name", ""),
                "created_timestamp": time.time(),
                "market_cap": 0,
                "source": "meteora",
            })
        return result
    except Exception as e:
        await audit(f"Meteora scanner error: {e}", "ERROR")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Filter logic
# ─────────────────────────────────────────────────────────────────────────────

async def _passes_scanner_filter(token: dict) -> tuple[bool, str]:
    """
    Apply quick filters before full safety check.
    Returns (passes, reason_if_not).
    """
    mint = token.get("mint", "")
    if not mint:
        return False, "No mint address"

    if mint in _seen_tokens:
        return False, "Already seen"

    # Age filter: must be at least 5 minutes old (allow bonding curve to stabilise)
    created = token.get("created_timestamp", 0)
    age_sec = time.time() - (created / 1000 if created > 1e10 else created)
    if age_sec < 300:   # 5 minutes
        return False, f"Too new ({age_sec:.0f}s old — waiting for 5min min)"

    # Market cap filter (pump.fun only)
    mcap = token.get("market_cap", 0)
    if token.get("source") == "pump.fun" and mcap > 0:
        if mcap > 10_000_000:   # Skip if already >$10M mcap
            return False, f"Market cap too high (${mcap:,.0f})"

    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# Main scanner loop
# ─────────────────────────────────────────────────────────────────────────────

async def run_scanner(notify_callback) -> None:
    """
    Background task — polls for new tokens and triggers buy flow.
    Only runs when bot is active and scanner is enabled.
    """
    global _seen_tokens

    await audit("Token scanner started", "INFO")

    while True:
        try:
            state = BotState.get()

            if not state.get("running") or state.get("kill_switch"):
                await asyncio.sleep(config.SCANNER_INTERVAL_SEC)
                continue

            scanner_enabled = await get_setting("scanner_enabled", False)
            if not scanner_enabled:
                await asyncio.sleep(config.SCANNER_INTERVAL_SEC)
                continue

            # Gather from all sources concurrently
            pump_tokens, raydium_tokens, meteora_tokens = await asyncio.gather(
                fetch_new_pump_tokens(),
                fetch_new_raydium_pools(),
                fetch_meteora_new_pools(),
            )

            all_tokens = pump_tokens + raydium_tokens + meteora_tokens

            for token in all_tokens:
                mint = token.get("mint", "")
                if not mint:
                    continue

                passes, reason = await _passes_scanner_filter(token)
                if not passes:
                    if mint not in _seen_tokens:
                        _seen_tokens.add(mint)
                    continue

                _seen_tokens.add(mint)

                symbol = token.get("symbol", mint[:8])
                source_label = token.get("source", "scanner")

                await audit(
                    f"Scanner found: {symbol} ({mint[:8]}…) from {source_label}", "INFO"
                )
                await notify_callback(
                    f"🔍 Scanner: New token `{symbol}` from *{source_label}*\n`{mint}`"
                )

                # Trigger full buy flow
                await full_buy_flow(
                    mint=mint,
                    source="scanner",
                    notify_callback=notify_callback,
                )

        except Exception as e:
            await audit(f"Scanner loop error: {e}", "ERROR")

        await asyncio.sleep(config.SCANNER_INTERVAL_SEC)


# ─────────────────────────────────────────────────────────────────────────────
# Reset seen tokens (useful for testing / /resetday)
# ─────────────────────────────────────────────────────────────────────────────

def reset_seen_tokens() -> None:
    global _seen_tokens
    _seen_tokens = set()
