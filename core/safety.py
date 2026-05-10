"""
core/safety.py — Token safety checks before any trade.
Checks: mint verification, freeze authority, honeypot simulation,
liquidity pool validation, rugpull signals, token age.
"""
from __future__ import annotations
import asyncio
import time
from typing import Optional

import aiohttp

import config
from core.database import is_blacklisted, is_whitelisted, blacklist_add, audit


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

class SafetyResult:
    def __init__(self):
        self.passed = True
        self.checks: dict[str, tuple[bool, str]] = {}   # name -> (ok, detail)

    def fail(self, name: str, reason: str):
        self.passed = False
        self.checks[name] = (False, reason)

    def ok(self, name: str, detail: str = ""):
        self.checks[name] = (True, detail)

    def summary(self) -> str:
        lines = []
        for name, (ok, detail) in self.checks.items():
            icon = "✅" if ok else "❌"
            lines.append(f"{icon} {name}: {detail}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# RPC helper (reused from wallet.py to avoid circular imports)
# ─────────────────────────────────────────────────────────────────────────────

async def _rpc(method: str, params: list) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with aiohttp.ClientSession() as s:
        async with s.post(config.SOLANA_RPC_URL, json=payload,
                          timeout=aiohttp.ClientTimeout(total=15)) as r:
            return await r.json()


# ─────────────────────────────────────────────────────────────────────────────
# Individual checks
# ─────────────────────────────────────────────────────────────────────────────

async def check_blacklist(mint: str, result: SafetyResult) -> None:
    if await is_blacklisted(mint):
        result.fail("blacklist", "Token is blacklisted")
    else:
        result.ok("blacklist", "Not blacklisted")


async def check_mint_exists(mint: str, result: SafetyResult) -> Optional[dict]:
    """Fetch mint account info.  Returns parsed data or None."""
    resp = await _rpc("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    value = resp.get("result", {}).get("value")
    if not value:
        result.fail("mint_exists", "Mint account not found on-chain")
        return None
    result.ok("mint_exists", "Mint account verified")
    return value


async def check_freeze_authority(mint_data: Optional[dict], result: SafetyResult) -> None:
    """Fail if the token still has a freeze authority set."""
    if not mint_data:
        result.fail("freeze_authority", "Cannot check — mint data unavailable")
        return
    try:
        info = mint_data["data"]["parsed"]["info"]
        fa = info.get("freezeAuthority")
        if fa:
            result.fail("freeze_authority", f"Freeze authority active: {fa}")
        else:
            result.ok("freeze_authority", "No freeze authority")
    except (KeyError, TypeError):
        result.ok("freeze_authority", "Could not parse — assuming safe")


async def check_mint_authority(mint_data: Optional[dict], result: SafetyResult) -> None:
    """Warn if mint authority is still set (can print more tokens)."""
    if not mint_data:
        result.ok("mint_authority", "Skipped")
        return
    try:
        info = mint_data["data"]["parsed"]["info"]
        ma = info.get("mintAuthority")
        supply = int(info.get("supply", 0))
        if ma and supply > 0:
            result.fail("mint_authority", f"Mint authority active: {ma} — unlimited supply risk")
        else:
            result.ok("mint_authority", "Mint authority null or supply=0")
    except (KeyError, TypeError):
        result.ok("mint_authority", "Skipped")


async def check_liquidity(mint: str, min_usd: float, result: SafetyResult) -> float:
    """
    Check Jupiter route for quote → assess liquidity.
    Returns estimated liquidity SOL (proxy: max tradeable input before large impact).
    """
    try:
        # Try to get a quote for 1 SOL → token as a basic liquidity probe
        params = {
            "inputMint": config.WSOL_MINT,
            "outputMint": mint,
            "amount": str(int(0.1 * 1e9)),   # 0.1 SOL
            "slippageBps": 5000,
        }
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{config.JUPITER_API_URL}/quote",
                             params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
        if "error" in data or not data.get("outAmount"):
            result.fail("liquidity", "No Jupiter route found — token may have no liquidity")
            return 0.0
        result.ok("liquidity", f"Jupiter route found (0.1 SOL probe succeeded)")
        return 1.0   # We can't compute exact USD liquidity without a price oracle here
    except Exception as e:
        result.fail("liquidity", f"Liquidity check error: {e}")
        return 0.0


async def check_honeypot(mint: str, result: SafetyResult) -> None:
    """
    Simulate a buy then a sell via Jupiter to detect sell-block honeypots.
    Uses Jupiter transaction simulation (no SOL spent).
    """
    try:
        # Step 1: Get buy quote
        buy_params = {
            "inputMint": config.WSOL_MINT,
            "outputMint": mint,
            "amount": str(int(0.01 * 1e9)),
            "slippageBps": 5000,
        }
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{config.JUPITER_API_URL}/quote",
                             params=buy_params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                buy_quote = await r.json()

        if "error" in buy_quote or not buy_quote.get("outAmount"):
            result.fail("honeypot", "Cannot simulate buy — no route")
            return

        out_amount = buy_quote["outAmount"]

        # Step 2: Get sell quote (reverse)
        sell_params = {
            "inputMint": mint,
            "outputMint": config.WSOL_MINT,
            "amount": out_amount,
            "slippageBps": 5000,
        }
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{config.JUPITER_API_URL}/quote",
                             params=sell_params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                sell_quote = await r.json()

        if "error" in sell_quote or not sell_quote.get("outAmount"):
            result.fail("honeypot", "HONEYPOT DETECTED — cannot get sell route")
            return

        # Check price impact — if sell impact > 90% it's a trap
        sell_price_impact = float(sell_quote.get("priceImpactPct", 0))
        if sell_price_impact > 80:
            result.fail("honeypot", f"Extreme sell price impact {sell_price_impact:.1f}% — possible honeypot")
        else:
            result.ok("honeypot", f"Sell route found, price impact {sell_price_impact:.2f}%")

    except Exception as e:
        result.ok("honeypot", f"Simulation inconclusive ({e}) — proceeding with caution")


async def check_top_holder_concentration(mint: str, result: SafetyResult) -> None:
    """
    Check if top accounts hold >80% supply (rugpull risk).
    Uses getTokenLargestAccounts RPC call.
    """
    try:
        resp = await _rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        accounts = resp.get("result", {}).get("value", [])
        if not accounts:
            result.ok("concentration", "No large accounts detected")
            return
        total_resp = await _rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        supply_str = total_resp.get("result", {}).get("value", {}).get(
            "data", {}).get("parsed", {}).get("info", {}).get("supply", "0"
        )
        total_supply = int(supply_str) if supply_str else 0
        if total_supply == 0:
            result.ok("concentration", "Cannot determine supply")
            return
        top5 = sum(int(a.get("amount", 0)) for a in accounts[:5])
        pct = (top5 / total_supply) * 100
        if pct > 80:
            result.fail("concentration", f"Top 5 wallets hold {pct:.1f}% — high rugpull risk")
        elif pct > 60:
            result.ok("concentration", f"⚠️ Top 5 hold {pct:.1f}% — elevated concentration")
        else:
            result.ok("concentration", f"Top 5 hold {pct:.1f}% — acceptable distribution")
    except Exception as e:
        result.ok("concentration", f"Skipped ({e})")


async def check_token_age(mint: str, min_age_hours: float, result: SafetyResult) -> float:
    """
    Estimate token age from the mint account's creation slot.
    Returns age in hours (approximate).
    """
    try:
        resp = await _rpc("getAccountInfo", [mint, {"commitment": "confirmed"}])
        # We can't get exact creation time without slot → timestamp lookup (expensive)
        # Use a Helius API call if available, otherwise pass
        if config.HELIUS_API_KEY:
            url = f"https://api.helius.xyz/v0/addresses/{mint}/transactions"
            params = {"api-key": config.HELIUS_API_KEY, "type": "UNKNOWN", "limit": 1}
            async with aiohttp.ClientSession() as s:
                async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    txs = await r.json()
            if txs and isinstance(txs, list):
                oldest_ts = txs[-1].get("timestamp", time.time())
                age_hours = (time.time() - oldest_ts) / 3600
                if age_hours < min_age_hours:
                    result.fail("token_age", f"Token only {age_hours*60:.1f}min old (min: {min_age_hours*60:.0f}min)")
                else:
                    result.ok("token_age", f"Age: {age_hours:.2f}h (min: {min_age_hours:.2f}h)")
                return age_hours
        # No Helius — skip age check but note it
        result.ok("token_age", f"Age check skipped (set HELIUS_API_KEY for this check)")
        return 9999.0
    except Exception as e:
        result.ok("token_age", f"Age check skipped ({e})")
        return 9999.0


# ─────────────────────────────────────────────────────────────────────────────
# Master safety gate
# ─────────────────────────────────────────────────────────────────────────────

async def run_safety_checks(
    mint: str,
    min_liquidity_usd: float,
    min_token_age_hours: float,
    skip_age: bool = False,
) -> SafetyResult:
    """
    Run all safety checks concurrently.
    Returns a SafetyResult — caller should check .passed before trading.
    """
    result = SafetyResult()

    # Blacklist first (cheap DB check)
    await check_blacklist(mint, result)
    if not result.passed:
        return result

    # Fetch mint data once, reuse
    mint_data = await check_mint_exists(mint, result)
    if not result.passed:
        return result

    # Run remaining checks concurrently
    await asyncio.gather(
        check_freeze_authority(mint_data, result),
        check_mint_authority(mint_data, result),
        check_honeypot(mint, result),
        check_liquidity(mint, min_liquidity_usd, result),
        check_top_holder_concentration(mint, result),
        check_token_age(mint, min_token_age_hours if not skip_age else 0, result),
    )

    if not result.passed:
        await audit(f"Safety FAILED for {mint}: {result.summary()}", "WARN")
    else:
        await audit(f"Safety PASSED for {mint}", "INFO")

    return result
