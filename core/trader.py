"""
core/trader.py — Core trading engine.
Handles: buy execution, sell execution, position monitoring,
take-profit, trailing stop, daily limits, cooldown, paper trading.
"""
from __future__ import annotations
import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

import config
from core import database as db
from core.jupiter import (
    get_quote, get_swap_transaction, simulate_transaction,
    send_transaction, confirm_transaction, get_token_price_sol,
    get_token_info, get_sol_price_usd,
)
from core.safety import run_safety_checks
from core.wallet import get_keypair, get_sol_balance, get_token_balance
from utils.state import BotState


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _within_trading_window() -> bool:
    window_start = await db.get_setting("trading_window_start", config.DEFAULT_TRADING_WINDOW_START)
    window_end   = await db.get_setting("trading_window_end",   config.DEFAULT_TRADING_WINDOW_END)
    now_utc = datetime.now(timezone.utc).strftime("%H:%M")
    return window_start <= now_utc <= window_end


async def _daily_trade_count() -> int:
    stats = await db.get_today_stats()
    return stats.get("trades", 0)


async def _daily_loss() -> float:
    stats = await db.get_today_stats()
    pnl = stats.get("pnl_sol", 0.0)
    return abs(pnl) if pnl < 0 else 0.0


async def _already_has_position(mint: str) -> bool:
    wallets = await db.get_all_wallets()
    for w in wallets:
        if await db.get_position(w["label"], mint):
            return True
    return False


def _format_sig(sig: str) -> str:
    return f"https://solscan.io/tx/{sig}"


# ─────────────────────────────────────────────────────────────────────────────
# Pre-trade gate
# ─────────────────────────────────────────────────────────────────────────────

class TradeSkipReason(Exception):
    """Raised when a trade should be skipped (not an error)."""
    pass


async def pre_trade_checks(mint: str, source: str = "signal") -> None:
    """
    Run all pre-trade gates.  Raises TradeSkipReason with a descriptive
    message if any gate fails so the caller can log & notify.
    """
    state = BotState.get()

    if state.get("kill_switch"):
        raise TradeSkipReason("🔴 Kill switch is active — trading halted.")

    if not state.get("running") and source != "manual":
        raise TradeSkipReason("⏸ Bot is stopped — not taking new trades.")

    if state.get("paused_until") and time.time() < state["paused_until"]:
        remaining = int(state["paused_until"] - time.time())
        raise TradeSkipReason(f"⏸ Bot paused — {remaining}s remaining.")

    # Cooldown check
    last_trade = state.get("last_trade_time", 0)
    cooldown = await db.get_setting("cooldown_sec", config.DEFAULT_COOLDOWN_SEC)
    if time.time() - last_trade < cooldown:
        wait = int(cooldown - (time.time() - last_trade))
        raise TradeSkipReason(f"⏳ Cooldown — {wait}s remaining before next trade.")

    # Trading window
    if not await _within_trading_window():
        ws = await db.get_setting("trading_window_start", config.DEFAULT_TRADING_WINDOW_START)
        we = await db.get_setting("trading_window_end",   config.DEFAULT_TRADING_WINDOW_END)
        raise TradeSkipReason(f"🕐 Outside trading window ({ws}–{we} UTC).")

    # Daily trade cap
    max_daily = await db.get_setting("max_daily_trades", config.DEFAULT_MAX_DAILY_TRADES)
    trades_today = await _daily_trade_count()
    if trades_today >= max_daily:
        raise TradeSkipReason(f"📊 Daily trade limit reached ({trades_today}/{max_daily}).")

    # Daily loss cap
    daily_loss_cap = await db.get_setting("daily_loss_cap", config.DEFAULT_DAILY_LOSS_CAP)
    daily_loss = await _daily_loss()
    if daily_loss >= daily_loss_cap:
        raise TradeSkipReason(f"💸 Daily loss cap hit ({daily_loss:.4f}/{daily_loss_cap} SOL).")

    # Duplicate signal prevention
    if await _already_has_position(mint):
        raise TradeSkipReason(f"🔁 Already holding position for {mint[:8]}… — skipping duplicate signal.")


# ─────────────────────────────────────────────────────────────────────────────
# Buy
# ─────────────────────────────────────────────────────────────────────────────

async def execute_buy(
    mint: str,
    wallet_label: str,
    sol_amount: Optional[float] = None,
    slippage_bps: Optional[int] = None,
    take_profit_x: Optional[float] = None,
    trailing_stop_pct: Optional[float] = None,
    paper: bool = False,
    source: str = "signal",
) -> dict:
    """
    Execute a buy order.
    Returns a result dict with keys: success, message, tx, sol_spent, token_amount, entry_price_sol
    """
    result = {
        "success": False,
        "message": "",
        "tx": "",
        "sol_spent": 0.0,
        "token_amount": 0.0,
        "entry_price_sol": 0.0,
        "symbol": "???",
    }

    # Resolve parameters
    if slippage_bps is None:
        slippage_bps = await db.get_setting("slippage_bps", config.DEFAULT_SLIPPAGE_BPS)
    if take_profit_x is None:
        take_profit_x = await db.get_setting("take_profit", config.DEFAULT_TAKE_PROFIT)
    if trailing_stop_pct is None:
        trailing_stop_pct = await db.get_setting("trailing_stop", config.DEFAULT_TRAILING_STOP)

    # Wallet info
    wallet = await db.get_wallet(wallet_label)
    if not wallet or not wallet["enabled"]:
        result["message"] = f"Wallet '{wallet_label}' not found or disabled."
        return result

    pubkey = wallet["pubkey"]

    # Balance check
    balance = await get_sol_balance(pubkey)
    if sol_amount is None:
        position_pct = await db.get_setting("position_pct", config.DEFAULT_POSITION_PCT)
        sol_amount = balance * (position_pct / 100)

    # Keep 0.01 SOL for fees
    if balance < sol_amount + 0.01:
        result["message"] = (
            f"❌ Insufficient balance — wallet has {balance:.4f} SOL, "
            f"need {sol_amount + 0.01:.4f} SOL (including fees)."
        )
        await db.audit(result["message"], "WARN")
        return result

    if sol_amount < 0.001:
        result["message"] = "❌ Trade amount too small (< 0.001 SOL)."
        return result

    # Token info
    token_info = await get_token_info(mint)
    symbol = token_info.get("symbol", mint[:8])
    result["symbol"] = symbol

    if paper:
        # Paper trade — simulate without sending
        quote = await get_quote(config.WSOL_MINT, mint, int(sol_amount * 1e9), slippage_bps)
        if not quote:
            result["message"] = f"❌ No Jupiter route for {symbol} — cannot paper trade."
            return result
        out_amount = int(quote["outAmount"])
        entry_price = (sol_amount * 1e9) / out_amount if out_amount else 0
        await db.open_position(
            wallet_label, mint, symbol, entry_price, out_amount / 1e9,
            sol_amount, take_profit_x, trailing_stop_pct, "PAPER", True, source,
        )
        BotState.update({"last_trade_time": time.time()})
        result.update({
            "success": True,
            "message": f"📝 Paper BUY {symbol}: {sol_amount:.4f} SOL → {out_amount/1e9:.2f} tokens",
            "sol_spent": sol_amount,
            "token_amount": out_amount / 1e9,
            "entry_price_sol": entry_price,
        })
        await db.audit(f"PAPER BUY {symbol} ({mint[:8]}) — {sol_amount:.4f} SOL", "TRADE")
        return result

    # Real trade
    lamports = int(sol_amount * 1e9)
    quote = await get_quote(config.WSOL_MINT, mint, lamports, slippage_bps)
    if not quote:
        result["message"] = f"❌ No Jupiter route found for {symbol}. Token may have no liquidity or be unlisted."
        await db.audit(result["message"], "WARN")
        return result

    swap_tx = await get_swap_transaction(quote, pubkey)
    if not swap_tx:
        result["message"] = f"❌ Failed to build swap transaction for {symbol}."
        await db.audit(result["message"], "ERROR")
        return result

    # Simulate first
    sim_ok, sim_msg = await simulate_transaction(swap_tx)
    if not sim_ok:
        result["message"] = f"❌ Transaction simulation failed for {symbol}: {sim_msg}"
        await db.audit(result["message"], "WARN")
        return result

    # Sign & send
    keypair = await get_keypair(wallet_label)
    ok, sig = await send_transaction(swap_tx, keypair)
    if not ok:
        result["message"] = f"❌ Transaction failed for {symbol}: {sig}"
        await db.audit(result["message"], "ERROR")
        return result

    # Confirm
    confirmed = await confirm_transaction(sig, max_retries=25)
    if not confirmed:
        result["message"] = f"⚠️ Transaction sent but not confirmed for {symbol}. Sig: {sig}"
        await db.audit(result["message"], "WARN")
        # Still record position tentatively
        pass

    out_amount = int(quote["outAmount"])
    entry_price = (sol_amount * 1e9) / out_amount if out_amount else 0

    await db.open_position(
        wallet_label, mint, symbol, entry_price, out_amount / 1e9,
        sol_amount, take_profit_x, trailing_stop_pct, sig, False, source,
    )
    BotState.update({"last_trade_time": time.time()})

    result.update({
        "success": True,
        "message": (
            f"✅ BUY {symbol}\n"
            f"💰 Spent: {sol_amount:.4f} SOL\n"
            f"🎯 Received: {out_amount/1e9:.4f} tokens\n"
            f"📈 Take profit: {take_profit_x}×\n"
            f"🔗 {_format_sig(sig)}"
        ),
        "tx": sig,
        "sol_spent": sol_amount,
        "token_amount": out_amount / 1e9,
        "entry_price_sol": entry_price,
    })
    await db.audit(f"BUY {symbol} ({mint[:8]}) — {sol_amount:.4f} SOL — tx: {sig}", "TRADE")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Sell
# ─────────────────────────────────────────────────────────────────────────────

async def execute_sell(
    position: dict,
    reason: str = "take_profit",
    paper: bool = False,
) -> dict:
    """
    Execute a full sell of an open position.
    Returns result dict.
    """
    result = {"success": False, "message": "", "tx": "", "sol_received": 0.0, "pnl_sol": 0.0}

    mint = position["mint"]
    wallet_label = position["wallet_label"]
    symbol = position.get("symbol", mint[:8])
    sol_spent = position["sol_spent"]
    token_amount = position["token_amount"]
    entry_price = position["entry_price_sol"]

    slippage_bps = await db.get_setting("slippage_bps", config.DEFAULT_SLIPPAGE_BPS)
    wallet = await db.get_wallet(wallet_label)
    if not wallet:
        result["message"] = f"Wallet '{wallet_label}' not found."
        return result
    pubkey = wallet["pubkey"]

    if paper:
        # Paper sell
        quote = await get_quote(mint, config.WSOL_MINT, int(token_amount * 1e9), slippage_bps)
        sol_received = int(quote["outAmount"]) / 1e9 if quote else sol_spent
        pnl = sol_received - sol_spent
        await db.close_position(wallet_label, mint)
        await db.record_trade(
            wallet_label, mint, symbol, entry_price,
            (sol_received * 1e9) / (token_amount * 1e9) if token_amount else 0,
            token_amount, sol_spent, sol_received,
            "PAPER", "PAPER", position["opened_at"], True, reason, position.get("source", "signal"),
        )
        result.update({
            "success": True,
            "message": f"📝 Paper SELL {symbol}: {sol_received:.4f} SOL | PnL: {pnl:+.4f} SOL ({(pnl/sol_spent*100):+.1f}%)",
            "sol_received": sol_received,
            "pnl_sol": pnl,
        })
        await db.audit(f"PAPER SELL {symbol} — PnL: {pnl:+.4f} SOL — reason: {reason}", "TRADE")
        return result

    # Get actual token balance from chain (may differ slightly from recorded)
    actual_tokens = await get_token_balance(pubkey, mint)
    if actual_tokens < token_amount * 0.01:
        # Token balance essentially zero — position may already be sold
        await db.close_position(wallet_label, mint)
        result["message"] = f"⚠️ Token balance for {symbol} is near zero — position closed without sell tx."
        return result

    sell_amount = actual_tokens
    lamports_in = int(sell_amount * 1e9)
    if lamports_in < 1:
        result["message"] = f"❌ Token amount too small to sell for {symbol}."
        return result

    quote = await get_quote(mint, config.WSOL_MINT, lamports_in, slippage_bps)
    if not quote:
        result["message"] = f"❌ No sell route for {symbol}. Market may have dried up."
        await db.audit(result["message"], "ERROR")
        return result

    swap_tx = await get_swap_transaction(quote, pubkey)
    if not swap_tx:
        result["message"] = f"❌ Failed to build sell transaction for {symbol}."
        await db.audit(result["message"], "ERROR")
        return result

    sim_ok, sim_msg = await simulate_transaction(swap_tx)
    if not sim_ok:
        result["message"] = f"❌ Sell simulation failed for {symbol}: {sim_msg}"
        await db.audit(result["message"], "ERROR")
        return result

    keypair = await get_keypair(wallet_label)
    ok, sig = await send_transaction(swap_tx, keypair)
    if not ok:
        result["message"] = f"❌ Sell transaction failed for {symbol}: {sig}"
        await db.audit(result["message"], "ERROR")
        return result

    await confirm_transaction(sig, max_retries=25)
    sol_received = int(quote.get("outAmount", 0)) / 1e9
    pnl = sol_received - sol_spent
    exit_price = (sol_received * 1e9) / lamports_in if lamports_in else 0

    await db.close_position(wallet_label, mint)
    await db.record_trade(
        wallet_label, mint, symbol, entry_price, exit_price,
        sell_amount, sol_spent, sol_received,
        position.get("buy_tx", ""), sig, position["opened_at"],
        False, reason, position.get("source", "signal"),
    )

    # Auto-blacklist on stop-loss
    if reason == "stop_loss":
        auto_bl = await db.get_setting("auto_blacklist", True)
        if auto_bl:
            await db.blacklist_add(mint, f"Auto-blacklisted: stop-loss at {exit_price:.8f}")
            await db.audit(f"Auto-blacklisted {symbol} after stop-loss", "INFO")

    result.update({
        "success": True,
        "message": (
            f"{'🟢' if pnl >= 0 else '🔴'} SELL {symbol}\n"
            f"💰 Received: {sol_received:.4f} SOL\n"
            f"📊 PnL: {pnl:+.4f} SOL ({(pnl/sol_spent*100):+.1f}%)\n"
            f"📋 Reason: {reason}\n"
            f"🔗 {_format_sig(sig)}"
        ),
        "tx": sig,
        "sol_received": sol_received,
        "pnl_sol": pnl,
    })
    await db.audit(
        f"SELL {symbol} ({mint[:8]}) — PnL: {pnl:+.4f} SOL — reason: {reason} — tx: {sig}", "TRADE"
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Position monitor loop
# ─────────────────────────────────────────────────────────────────────────────

async def monitor_positions(notify_callback) -> None:
    """
    Background task — checks all open positions every PRICE_MONITOR_INTERVAL_SEC.
    Triggers auto-sell on take-profit or trailing stop.
    notify_callback(msg: str) is called with sell result messages.
    """
    while True:
        try:
            positions = await db.get_open_positions()
            for pos in positions:
                mint = pos["mint"]
                wallet_label = pos["wallet_label"]
                entry_price = pos["entry_price_sol"]
                take_profit_x = pos["take_profit_x"]
                trailing_stop = pos["trailing_stop"]
                peak_price = pos.get("peak_price_sol") or entry_price
                paper = bool(pos.get("paper", False))

                current_price = await get_token_price_sol(mint)
                if not current_price or current_price == 0:
                    continue

                # Update peak
                if current_price > peak_price:
                    await db.update_peak_price(wallet_label, mint, current_price)
                    peak_price = current_price

                multiplier = current_price / entry_price if entry_price else 0

                # Take profit check
                if multiplier >= take_profit_x:
                    res = await execute_sell(pos, reason="take_profit", paper=paper)
                    if res["success"]:
                        await notify_callback(res["message"])
                    continue

                # Trailing stop check
                if trailing_stop > 0 and peak_price > 0:
                    drop_pct = (peak_price - current_price) / peak_price * 100
                    if drop_pct >= trailing_stop:
                        res = await execute_sell(pos, reason="trailing_stop", paper=paper)
                        if res["success"]:
                            await notify_callback(res["message"])
                        continue

        except Exception as e:
            await db.audit(f"Position monitor error: {e}", "ERROR")

        await asyncio.sleep(config.PRICE_MONITOR_INTERVAL_SEC)


# ─────────────────────────────────────────────────────────────────────────────
# Full buy flow (with safety checks)
# ─────────────────────────────────────────────────────────────────────────────

async def full_buy_flow(
    mint: str,
    source: str = "signal",
    sol_amount: Optional[float] = None,
    notify_callback=None,
) -> None:
    """
    Full buy flow including pre-trade checks, safety checks, multi-wallet execution.
    notify_callback(msg: str) is called with results.
    """
    async def notify(msg: str):
        if notify_callback:
            await notify_callback(msg)
        await db.audit(msg)

    # Pre-trade gate
    try:
        await pre_trade_checks(mint, source)
    except TradeSkipReason as e:
        await notify(f"⏭ Trade skipped ({source}): {e}")
        return

    # Safety checks
    min_liq = await db.get_setting("min_liquidity_usd", config.DEFAULT_MIN_LIQUIDITY_USD)
    min_age = await db.get_setting("min_token_age_hours", config.DEFAULT_MIN_TOKEN_AGE_H)
    skip_age = source == "scanner"   # scanner tokens are intentionally new

    safety = await run_safety_checks(mint, min_liq, min_age, skip_age=skip_age)
    if not safety.passed:
        msg = f"🛡 Safety check FAILED for `{mint[:8]}…`\n{safety.summary()}"
        await notify(msg)
        return

    # Execute on all enabled wallets
    wallets = await db.get_all_wallets()
    active_wallets = [w for w in wallets if w["enabled"]]
    if not active_wallets:
        await notify("❌ No enabled wallets to trade with.")
        return

    paper = BotState.get().get("paper_mode", False)

    for wallet in active_wallets:
        res = await execute_buy(
            mint=mint,
            wallet_label=wallet["label"],
            sol_amount=sol_amount,
            paper=paper,
            source=source,
        )
        await notify(res["message"])


# ─────────────────────────────────────────────────────────────────────────────
# Kill switch — close all positions
# ─────────────────────────────────────────────────────────────────────────────

async def kill_all_positions(notify_callback=None) -> None:
    """Force-close every open position immediately."""
    positions = await db.get_open_positions()
    for pos in positions:
        res = await execute_sell(pos, reason="kill", paper=bool(pos.get("paper")))
        if notify_callback:
            await notify_callback(res["message"])
