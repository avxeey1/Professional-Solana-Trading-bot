"""
core/wallet.py — Solana wallet operations: create, import, balance, transfer.
Private keys are always encrypted at rest via utils/crypto.py.
"""
from __future__ import annotations
import asyncio
from typing import Optional

import aiohttp
import base58
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.transaction import Transaction
from solders.message import Message
from solders.hash import Hash

import config
from core.database import (
    add_wallet, get_wallet, get_all_wallets,
    toggle_wallet as db_toggle_wallet,
)
from utils.crypto import encrypt, decrypt


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _rpc(method: str, params: list) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with aiohttp.ClientSession() as session:
        async with session.post(config.SOLANA_RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            return await resp.json()


def _keypair_from_b58(b58_key: str) -> Keypair:
    raw = base58.b58decode(b58_key)
    return Keypair.from_bytes(raw)


# ─────────────────────────────────────────────────────────────────────────────
# Create / import
# ─────────────────────────────────────────────────────────────────────────────

async def create_wallet(label: str) -> dict:
    """Generate a new Solana keypair, encrypt & persist it."""
    kp = Keypair()
    privkey_b58 = base58.b58encode(bytes(kp)).decode()
    pubkey = str(kp.pubkey())
    enc = encrypt(privkey_b58)
    await add_wallet(label, pubkey, enc)
    return {"label": label, "pubkey": pubkey, "privkey_b58": privkey_b58}


async def import_wallet(label: str, privkey_b58: str) -> dict:
    """Import an existing wallet by base-58 private key."""
    kp = _keypair_from_b58(privkey_b58)
    pubkey = str(kp.pubkey())
    enc = encrypt(privkey_b58)
    await add_wallet(label, pubkey, enc)
    return {"label": label, "pubkey": pubkey}


async def get_keypair(label: str) -> Keypair:
    """Decrypt and return a Keypair for signing."""
    row = await get_wallet(label)
    if not row:
        raise ValueError(f"Wallet '{label}' not found.")
    privkey_b58 = decrypt(row["enc_privkey"])
    return _keypair_from_b58(privkey_b58)


async def export_privkey(label: str) -> str:
    """Return raw base-58 private key (for /exportkey — use with caution)."""
    row = await get_wallet(label)
    if not row:
        raise ValueError(f"Wallet '{label}' not found.")
    return decrypt(row["enc_privkey"])


# ─────────────────────────────────────────────────────────────────────────────
# Balances
# ─────────────────────────────────────────────────────────────────────────────

async def get_sol_balance(pubkey: str) -> float:
    """Return SOL balance for a public key."""
    result = await _rpc("getBalance", [pubkey, {"commitment": "confirmed"}])
    lamports = result.get("result", {}).get("value", 0)
    return lamports / 1e9


async def get_all_balances() -> list[dict]:
    """Return [{label, pubkey, balance_sol, enabled}] for all wallets."""
    wallets = await get_all_wallets()
    results = []
    for w in wallets:
        try:
            bal = await get_sol_balance(w["pubkey"])
        except Exception:
            bal = -1.0
        results.append({
            "label": w["label"],
            "pubkey": w["pubkey"],
            "balance_sol": bal,
            "enabled": bool(w["enabled"]),
        })
    return results


async def get_enabled_wallets() -> list[dict]:
    wallets = await get_all_wallets()
    return [w for w in wallets if w["enabled"]]


# ─────────────────────────────────────────────────────────────────────────────
# Toggle
# ─────────────────────────────────────────────────────────────────────────────

async def toggle_wallet(label: str) -> bool:
    return await db_toggle_wallet(label)


# ─────────────────────────────────────────────────────────────────────────────
# Transfer SOL
# ─────────────────────────────────────────────────────────────────────────────

async def transfer_sol(from_label: str, to_pubkey_str: str, amount_sol: float) -> str:
    """Sign and send a SOL transfer.  Returns transaction signature."""
    kp = await get_keypair(from_label)
    sender = kp.pubkey()
    receiver = Pubkey.from_string(to_pubkey_str)
    lamports = int(amount_sol * 1e9)

    # Fetch recent blockhash
    bh_resp = await _rpc("getLatestBlockhash", [{"commitment": "finalized"}])
    blockhash_str = bh_resp["result"]["value"]["blockhash"]
    recent_blockhash = Hash.from_string(blockhash_str)

    ix = transfer(TransferParams(from_pubkey=sender, to_pubkey=receiver, lamports=lamports))
    msg = Message.new_with_blockhash([ix], sender, recent_blockhash)
    tx = Transaction([kp], msg, recent_blockhash)

    raw = bytes(tx)
    import base64 as b64
    encoded = b64.b64encode(raw).decode()

    result = await _rpc("sendTransaction", [encoded, {"encoding": "base64", "skipPreflight": False}])
    if "error" in result:
        raise RuntimeError(f"Transfer failed: {result['error']}")
    return result["result"]


# ─────────────────────────────────────────────────────────────────────────────
# Token balance helper (used by trader)
# ─────────────────────────────────────────────────────────────────────────────

async def get_token_balance(pubkey: str, mint: str) -> float:
    """Return SPL token balance for a wallet pubkey & mint."""
    result = await _rpc(
        "getTokenAccountsByOwner",
        [pubkey, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    )
    accounts = result.get("result", {}).get("value", [])
    if not accounts:
        return 0.0
    amt = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
    return float(amt.get("uiAmount") or 0)


async def get_token_accounts(pubkey: str) -> list[dict]:
    """Return all SPL token accounts for a wallet."""
    result = await _rpc(
        "getTokenAccountsByOwner",
        [pubkey, {"programId": config.TOKEN_PROGRAM_ID},
         {"encoding": "jsonParsed", "commitment": "confirmed"}],
    )
    accounts = result.get("result", {}).get("value", [])
    out = []
    for acc in accounts:
        info = acc["account"]["data"]["parsed"]["info"]
        out.append({
            "mint": info["mint"],
            "amount": float(info["tokenAmount"].get("uiAmount") or 0),
            "decimals": info["tokenAmount"]["decimals"],
        })
    return out
