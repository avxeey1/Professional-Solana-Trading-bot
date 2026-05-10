"""
core/jupiter.py — Jupiter Aggregator v6 integration.
Handles: quotes, swap transaction building, simulation, execution.
"""
from __future__ import annotations
import asyncio
import base64
from typing import Optional

import aiohttp
import base58
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

import config
from core.database import audit


# ─────────────────────────────────────────────────────────────────────────────
# Quote
# ─────────────────────────────────────────────────────────────────────────────

async def get_quote(
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int = 500,
) -> Optional[dict]:
    """
    Fetch a Jupiter quote.
    Returns the full quote object or None on failure.
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_lamports),
        "slippageBps": str(slippage_bps),
        "onlyDirectRoutes": "false",
        "asLegacyTransaction": "false",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{config.JUPITER_API_URL}/quote",
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = await r.json()
        if "error" in data:
            await audit(f"Jupiter quote error: {data['error']}", "ERROR")
            return None
        return data
    except Exception as e:
        await audit(f"Jupiter quote exception: {e}", "ERROR")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Swap transaction
# ─────────────────────────────────────────────────────────────────────────────

async def get_swap_transaction(
    quote: dict,
    user_pubkey: str,
    wrap_unwrap_sol: bool = True,
    priority_fee_lamports: int = 1_000,       # 0.000001 SOL priority fee
) -> Optional[str]:
    """
    POST to Jupiter /swap to get a base64 VersionedTransaction.
    Returns base64 string or None.
    """
    body = {
        "quoteResponse": quote,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": wrap_unwrap_sol,
        "computeUnitPriceMicroLamports": priority_fee_lamports,
        "dynamicComputeUnitLimit": True,
        "asLegacyTransaction": False,
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{config.JUPITER_API_URL}/swap",
                json=body,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                data = await r.json()
        if "error" in data:
            await audit(f"Jupiter swap error: {data['error']}", "ERROR")
            return None
        return data.get("swapTransaction")
    except Exception as e:
        await audit(f"Jupiter swap exception: {e}", "ERROR")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Simulation
# ─────────────────────────────────────────────────────────────────────────────

async def simulate_transaction(tx_b64: str) -> tuple[bool, str]:
    """
    Simulate a transaction via RPC without broadcasting.
    Returns (success, message).
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "simulateTransaction",
        "params": [
            tx_b64,
            {
                "encoding": "base64",
                "sigVerify": False,
                "commitment": "confirmed",
                "replaceRecentBlockhash": True,
            },
        ],
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                config.SOLANA_RPC_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = await r.json()
        result = data.get("result", {}).get("value", {})
        err = result.get("err")
        logs = result.get("logs", [])
        if err:
            log_str = " | ".join(logs[-5:]) if logs else str(err)
            return False, f"Simulation failed: {err} — {log_str}"
        return True, "Simulation passed"
    except Exception as e:
        return False, f"Simulation exception: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Send transaction
# ─────────────────────────────────────────────────────────────────────────────

async def send_transaction(tx_b64: str, keypair: Keypair) -> tuple[bool, str]:
    """
    Sign and broadcast a Jupiter VersionedTransaction.
    Returns (success, signature_or_error).
    """
    try:
        raw_bytes = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(raw_bytes)
        # Sign with keypair
        signed = VersionedTransaction(tx.message, [keypair])
        signed_b64 = base64.b64encode(bytes(signed)).decode()

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                signed_b64,
                {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "maxRetries": 3,
                    "preflightCommitment": "confirmed",
                },
            ],
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(
                config.SOLANA_RPC_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                data = await r.json()

        if "error" in data:
            return False, str(data["error"])
        sig = data.get("result", "")
        return True, sig

    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# Confirm transaction
# ─────────────────────────────────────────────────────────────────────────────

async def confirm_transaction(signature: str, max_retries: int = 20) -> bool:
    """Poll for transaction confirmation.  Returns True if confirmed."""
    for _ in range(max_retries):
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignatureStatuses",
                "params": [[signature], {"searchTransactionHistory": True}],
            }
            async with aiohttp.ClientSession() as s:
                async with s.post(config.SOLANA_RPC_URL, json=payload,
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
            statuses = data.get("result", {}).get("value", [])
            if statuses and statuses[0]:
                status = statuses[0]
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    if status.get("err") is None:
                        return True
                    return False  # confirmed but errored
        except Exception:
            pass
        await asyncio.sleep(2)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Price helper
# ─────────────────────────────────────────────────────────────────────────────

async def get_token_price_sol(mint: str) -> Optional[float]:
    """
    Get approximate token price in SOL via Jupiter quote (0.01 SOL probe).
    Returns SOL per token or None.
    """
    quote = await get_quote(
        input_mint=config.WSOL_MINT,
        output_mint=mint,
        amount_lamports=int(0.01 * 1e9),
        slippage_bps=10000,
    )
    if not quote:
        return None
    out = int(quote.get("outAmount", 0))
    if out == 0:
        return None
    return (0.01 * 1e9) / out   # SOL per token (in raw decimals)


async def get_token_price_usd(mint: str, sol_usd: float) -> Optional[float]:
    """
    Returns token price in USD.
    Requires current SOL/USD price passed in.
    """
    price_sol = await get_token_price_sol(mint)
    if price_sol is None:
        return None
    return price_sol * sol_usd


async def get_sol_price_usd() -> float:
    """Fetch SOL/USD from Jupiter price API."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://price.jup.ag/v6/price",
                params={"ids": config.WSOL_MINT},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json()
        return float(data["data"][config.WSOL_MINT]["price"])
    except Exception:
        # Fallback — use a rough estimate if API fails
        return 150.0


async def get_token_info(mint: str) -> dict:
    """Get token symbol & name from Jupiter token list."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://token.jup.ag/strict",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                tokens = await r.json()
        for t in tokens:
            if t.get("address") == mint:
                return {"symbol": t.get("symbol", "???"), "name": t.get("name", "")}
    except Exception:
        pass
    # Try Helius as fallback
    if config.HELIUS_API_KEY:
        try:
            url = f"https://api.helius.xyz/v0/token-metadata"
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url,
                    json={"mintAccounts": [mint]},
                    params={"api-key": config.HELIUS_API_KEY},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    data = await r.json()
            if data and isinstance(data, list):
                meta = data[0].get("onChainMetadata", {}).get("metadata", {}).get("data", {})
                return {
                    "symbol": meta.get("symbol", "???").strip("\x00"),
                    "name": meta.get("name", "").strip("\x00"),
                }
        except Exception:
            pass
    return {"symbol": mint[:6] + "…", "name": ""}
