"""
handlers/commands.py — All Telegram command handlers.
Every handler checks TELEGRAM_OWNER_ID before executing.
"""
from __future__ import annotations
import asyncio
import time
import datetime
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

import config
from core import database as db
from core.jupiter import get_sol_price_usd, get_token_price_sol, get_token_info
from core.trader import (
    full_buy_flow, execute_sell, execute_buy, kill_all_positions, TradeSkipReason
)
from core.wallet import (
    create_wallet, import_wallet, get_all_balances, transfer_sol,
    export_privkey, toggle_wallet as wallet_toggle, get_sol_balance,
)
from core.scanner import reset_seen_tokens
from utils.state import BotState
from utils.parser import extract_first_address


# ─────────────────────────────────────────────────────────────────────────────
# Auth decorator
# ─────────────────────────────────────────────────────────────────────────────

def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != config.TELEGRAM_OWNER_ID:
            await update.message.reply_text("⛔ Unauthorised.")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


async def _reply(update: Update, text: str, md: bool = True) -> None:
    mode = ParseMode.MARKDOWN if md else None
    try:
        await update.message.reply_text(text, parse_mode=mode)
    except Exception:
        await update.message.reply_text(text[:4000])


# ─────────────────────────────────────────────────────────────────────────────
# ── Bot Control ───────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@owner_only
async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if BotState.is_killed():
        await _reply(update, "🔴 Kill switch is active. Use /revive first.")
        return
    BotState.update({"running": True, "paused_until": 0})
    await db.audit("Bot STARTED by owner", "INFO")
    await _reply(update, "✅ *Bot started* — auto-trading active.")


@owner_only
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    BotState.update({"running": False})
    await db.audit("Bot STOPPED by owner", "INFO")
    await _reply(update, "⏸ *Bot stopped* — no new trades will be taken.")


@owner_only
async def cmd_kill(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    BotState.update({"running": False, "kill_switch": True})
    await db.audit("KILL SWITCH activated by owner", "CRITICAL")
    await _reply(update, "🔴 *KILL SWITCH ACTIVATED*\nClosing all open positions…")
    async def notify(msg): await _reply(update, msg)
    await kill_all_positions(notify)
    await _reply(update, "All positions force-closed. Use /revive to re-enable.")


@owner_only
async def cmd_revive(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    BotState.update({"kill_switch": False, "running": False})
    await db.audit("Kill switch REVIVED by owner", "INFO")
    await _reply(update, "✅ Kill switch reset. Use /run to restart trading.")


@owner_only
async def cmd_paper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    current = BotState.is_paper()
    BotState.update({"paper_mode": not current})
    state = "ON 📝" if not current else "OFF 💰"
    await db.audit(f"Paper trading {state}", "INFO")
    await _reply(update, f"Paper trading: *{state}*")


@owner_only
async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mins = 60
    if ctx.args:
        try:
            mins = int(ctx.args[0])
        except ValueError:
            pass
    until = time.time() + mins * 60
    BotState.update({"paused_until": until})
    await db.audit(f"Bot paused for {mins} minutes", "INFO")
    await _reply(update, f"⏸ Bot paused for *{mins} minutes*.")


# ─────────────────────────────────────────────────────────────────────────────
# ── Info & Dashboard ──────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@owner_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = BotState.get()
    stats = await db.get_today_stats()
    sol_price = await get_sol_price_usd()
    wallets = await get_all_balances()
    total_sol = sum(w["balance_sol"] for w in wallets if w["balance_sol"] >= 0)
    positions = await db.get_open_positions()
    channels = await db.get_channels()
    running_icon = "🟢" if state["running"] else "🔴"
    kill_icon = "🔴 KILL" if state["kill_switch"] else ""
    paper_icon = "📝 PAPER" if state["paper_mode"] else ""
    paused_str = ""
    if state.get("paused_until") and time.time() < state["paused_until"]:
        rem = int(state["paused_until"] - time.time())
        paused_str = f"⏸ Paused {rem}s remaining\n"

    slippage = await db.get_setting("slippage_bps", config.DEFAULT_SLIPPAGE_BPS)
    position_pct = await db.get_setting("position_pct", config.DEFAULT_POSITION_PCT)
    take_profit = await db.get_setting("take_profit", config.DEFAULT_TAKE_PROFIT)
    trailing = await db.get_setting("trailing_stop", config.DEFAULT_TRAILING_STOP)
    max_trades = await db.get_setting("max_daily_trades", config.DEFAULT_MAX_DAILY_TRADES)
    loss_cap = await db.get_setting("daily_loss_cap", config.DEFAULT_DAILY_LOSS_CAP)
    scanner = await db.get_setting("scanner_enabled", False)

    msg = (
        f"*🤖 SolBot v{config.VERSION} Status*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{running_icon} Running: {'YES' if state['running'] else 'NO'}  {kill_icon} {paper_icon}\n"
        f"{paused_str}"
        f"⏱ Uptime: {BotState.uptime_str()}\n"
        f"🔍 Scanner: {'ON' if scanner else 'OFF'}\n\n"
        f"*💼 Wallets ({len(wallets)})*\n"
        f"💰 Total SOL: {total_sol:.4f} (≈${total_sol*sol_price:,.0f})\n"
        f"📊 SOL/USD: ${sol_price:,.2f}\n\n"
        f"*📈 Today ({stats.get('date','')})*\n"
        f"Trades: {stats.get('trades',0)}/{max_trades} | "
        f"W/L: {stats.get('wins',0)}/{stats.get('losses',0)}\n"
        f"PnL: {stats.get('pnl_sol',0):+.4f} SOL / Cap: {loss_cap} SOL\n\n"
        f"*⚙️ Parameters*\n"
        f"Slippage: {slippage}bps | Position: {position_pct}%\n"
        f"Take-profit: {take_profit}× | Trailing: {trailing}%\n\n"
        f"*📡 Signal Channels:* {len(channels)}\n"
        f"*🔓 Open Positions:* {len(positions)}\n"
    )
    await _reply(update, msg)


@owner_only
async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    positions = await db.get_open_positions()
    if not positions:
        await _reply(update, "📭 No open positions.")
        return
    sol_price = await get_sol_price_usd()
    lines = ["*📊 Open Positions*\n━━━━━━━━━━━━━━━━━━━━━"]
    for p in positions:
        current_price = await get_token_price_sol(p["mint"])
        entry = p["entry_price_sol"]
        if current_price and entry and entry > 0:
            mult = current_price / entry
            pnl_est = (current_price - entry) * p["token_amount"]
            pnl_str = f"PnL≈{pnl_est:+.4f} SOL ({mult:.2f}×)"
        else:
            pnl_str = "Price unavailable"
        label = "📝" if p.get("paper") else "💰"
        lines.append(
            f"{label} *{p.get('symbol','???')}* [{p['wallet_label']}]\n"
            f"  Spent: {p['sol_spent']:.4f} SOL | TP: {p['take_profit_x']}×\n"
            f"  {pnl_str}\n"
            f"  Mint: `{p['mint'][:16]}…`"
        )
    await _reply(update, "\n".join(lines))


@owner_only
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keys = [
        ("slippage_bps", config.DEFAULT_SLIPPAGE_BPS),
        ("position_pct", config.DEFAULT_POSITION_PCT),
        ("take_profit", config.DEFAULT_TAKE_PROFIT),
        ("trailing_stop", config.DEFAULT_TRAILING_STOP),
        ("cooldown_sec", config.DEFAULT_COOLDOWN_SEC),
        ("max_daily_trades", config.DEFAULT_MAX_DAILY_TRADES),
        ("daily_loss_cap", config.DEFAULT_DAILY_LOSS_CAP),
        ("min_liquidity_usd", config.DEFAULT_MIN_LIQUIDITY_USD),
        ("min_token_age_hours", config.DEFAULT_MIN_TOKEN_AGE_H),
        ("trading_window_start", config.DEFAULT_TRADING_WINDOW_START),
        ("trading_window_end", config.DEFAULT_TRADING_WINDOW_END),
        ("auto_blacklist", True),
        ("scanner_enabled", False),
        ("report_time", "08:00"),
    ]
    lines = ["*⚙️ Current Settings*\n━━━━━━━━━━━━━━━━━━━━━"]
    for key, default in keys:
        val = await db.get_setting(key, default)
        lines.append(f"`{key}`: {val}")
    await _reply(update, "\n".join(lines))


@owner_only
async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    logs = await db.get_audit_tail(config.LOG_LINES_TAIL)
    if not logs:
        await _reply(update, "📭 No logs yet.")
        return
    lines = ["*📋 Audit Log (last 20)*\n━━━━━━━━━━━━━━━━━━━━━"]
    for entry in logs:
        ts = datetime.datetime.fromtimestamp(entry["ts"], tz=datetime.timezone.utc)
        ts_str = ts.strftime("%m-%d %H:%M:%S")
        icon = {"INFO": "ℹ️", "WARN": "⚠️", "ERROR": "❌", "TRADE": "💱", "CRITICAL": "🚨"}.get(
            entry["level"], "•"
        )
        lines.append(f"{icon} `{ts_str}` {entry['message'][:80]}")
    await _reply(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# ── Wallets ───────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@owner_only
async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    balances = await get_all_balances()
    if not balances:
        await _reply(update, "📭 No wallets configured. Use /createwallet [label].")
        return
    sol_price = await get_sol_price_usd()
    lines = ["*💰 Wallet Balances*\n━━━━━━━━━━━━━━━━━━━━━"]
    total = 0.0
    for w in balances:
        icon = "✅" if w["enabled"] else "⏸"
        bal = w["balance_sol"]
        total += bal if bal >= 0 else 0
        lines.append(
            f"{icon} *{w['label']}*\n"
            f"  {bal:.4f} SOL (≈${bal*sol_price:,.2f})\n"
            f"  `{w['pubkey'][:20]}…`"
        )
    lines.append(f"\n*Total: {total:.4f} SOL (≈${total*sol_price:,.2f})*")
    await _reply(update, "\n".join(lines))


@owner_only
async def cmd_wallets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_balance(update, ctx)


@owner_only
async def cmd_createwallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    label = " ".join(ctx.args) if ctx.args else f"wallet_{int(time.time())}"
    result = await create_wallet(label)
    await db.audit(f"Wallet created: {label} ({result['pubkey'][:16]}…)", "INFO")
    await _reply(
        update,
        f"✅ *New wallet created*\n"
        f"Label: `{result['label']}`\n"
        f"Address: `{result['pubkey']}`\n\n"
        f"⚠️ Save your private key securely:\n"
        f"`{result['privkey_b58']}`\n\n"
        f"_This is the ONLY time the key will be shown._"
    )


@owner_only
async def cmd_importwallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/importwallet <base58_private_key> [label]`")
        return
    privkey = ctx.args[0]
    label = ctx.args[1] if len(ctx.args) > 1 else f"imported_{int(time.time())}"
    try:
        result = await import_wallet(label, privkey)
        await db.audit(f"Wallet imported: {label} ({result['pubkey'][:16]}…)", "INFO")
        await _reply(
            update,
            f"✅ *Wallet imported*\nLabel: `{result['label']}`\nAddress: `{result['pubkey']}`"
        )
    except Exception as e:
        await _reply(update, f"❌ Import failed: {e}")


@owner_only
async def cmd_togglewallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/togglewallet <label>`")
        return
    label = ctx.args[0]
    try:
        enabled = await wallet_toggle(label)
        status = "enabled ✅" if enabled else "disabled ⏸"
        await _reply(update, f"Wallet `{label}` is now *{status}*.")
    except Exception as e:
        await _reply(update, f"❌ {e}")


@owner_only
async def cmd_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 3:
        await _reply(update, "Usage: `/send <from_label> <to_pubkey> <sol_amount>`")
        return
    from_label, to_pubkey, amount_str = ctx.args[0], ctx.args[1], ctx.args[2]
    try:
        amount = float(amount_str)
        sig = await transfer_sol(from_label, to_pubkey, amount)
        await db.audit(f"Transfer: {from_label} → {to_pubkey[:8]}… {amount} SOL tx:{sig[:12]}…", "INFO")
        await _reply(
            update,
            f"✅ Sent *{amount} SOL*\nFrom: `{from_label}`\nTo: `{to_pubkey}`\n"
            f"🔗 https://solscan.io/tx/{sig}"
        )
    except Exception as e:
        await _reply(update, f"❌ Transfer failed: {e}")


@owner_only
async def cmd_receive(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    label = ctx.args[0] if ctx.args else None
    wallets = await get_all_balances()
    if not wallets:
        await _reply(update, "No wallets found.")
        return
    if label:
        target = next((w for w in wallets if w["label"] == label), None)
        if not target:
            await _reply(update, f"Wallet `{label}` not found.")
            return
        await _reply(update, f"📥 Deposit address for `{label}`:\n`{target['pubkey']}`")
    else:
        lines = ["*📥 Deposit Addresses*"]
        for w in wallets:
            lines.append(f"`{w['label']}`: `{w['pubkey']}`")
        await _reply(update, "\n".join(lines))


@owner_only
async def cmd_exportkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/exportkey <label>`")
        return
    label = ctx.args[0]
    try:
        key = await export_privkey(label)
        await db.audit(f"Private key exported for wallet: {label}", "WARN")
        await _reply(
            update,
            f"🔑 Private key for `{label}`:\n`{key}`\n\n"
            f"⚠️ *Keep this secret. Delete this message after saving.*"
        )
    except Exception as e:
        await _reply(update, f"❌ {e}")


# ─────────────────────────────────────────────────────────────────────────────
# ── Signal Channels ───────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@owner_only
async def cmd_channels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    channels = await db.get_channels()
    if not channels:
        await _reply(update, "📭 No signal channels configured.\nUse `/addchannel @username`")
        return
    lines = ["*📡 Signal Channels*\n━━━━━━━━━━━━━━━━━━━━━"]
    for ch in channels:
        lines.append(f"• `{ch['channel_id']}` {ch.get('label','')}")
    await _reply(update, "\n".join(lines))


@owner_only
async def cmd_addchannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/addchannel <@username or chat_id>`")
        return
    channel_id = ctx.args[0]
    label = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else ""
    await db.add_channel(channel_id, label)
    await db.audit(f"Channel added: {channel_id}", "INFO")
    await _reply(update, f"✅ Channel `{channel_id}` added to monitoring.")


@owner_only
async def cmd_removechannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/removechannel <@username or chat_id>`")
        return
    channel_id = ctx.args[0]
    await db.remove_channel(channel_id)
    await _reply(update, f"✅ Channel `{channel_id}` removed.")


# ─────────────────────────────────────────────────────────────────────────────
# ── Trading Parameters ────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@owner_only
async def cmd_setslippage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/setslippage <bps>` (e.g. 500 = 5%)")
        return
    try:
        val = int(ctx.args[0])
        if val < 1 or val > 10000:
            raise ValueError
        await db.set_setting("slippage_bps", val)
        await _reply(update, f"✅ Slippage set to *{val} bps* ({val/100:.1f}%)")
    except ValueError:
        await _reply(update, "❌ Invalid value. Must be 1–10000 bps.")


@owner_only
async def cmd_setposition(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/setposition <pct>` (e.g. 5 = 5% of balance)")
        return
    try:
        val = float(ctx.args[0])
        if val <= 0 or val > 100:
            raise ValueError
        await db.set_setting("position_pct", val)
        await _reply(update, f"✅ Position size set to *{val}%* of wallet balance.")
    except ValueError:
        await _reply(update, "❌ Invalid value. Must be 0.1–100.")


@owner_only
async def cmd_setprofit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/setprofit <multiplier>` (e.g. 2.0 = 2× = 100% gain)")
        return
    try:
        val = float(ctx.args[0])
        if val <= 1.0:
            raise ValueError("Must be > 1.0")
        await db.set_setting("take_profit", val)
        await _reply(update, f"✅ Take-profit set to *{val}×* ({(val-1)*100:.0f}% gain)")
    except ValueError as e:
        await _reply(update, f"❌ Invalid value: {e}")


@owner_only
async def cmd_settrailing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/settrailing <pct>` (0 = disabled, e.g. 20 = 20% drop from peak)")
        return
    try:
        val = float(ctx.args[0])
        await db.set_setting("trailing_stop", val)
        status = f"*{val}%* drop from peak" if val > 0 else "*disabled*"
        await _reply(update, f"✅ Trailing stop set to {status}")
    except ValueError:
        await _reply(update, "❌ Invalid value.")


@owner_only
async def cmd_setcooldown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/setcooldown <seconds>`")
        return
    try:
        val = int(ctx.args[0])
        await db.set_setting("cooldown_sec", val)
        await _reply(update, f"✅ Cooldown set to *{val} seconds*.")
    except ValueError:
        await _reply(update, "❌ Invalid value.")


@owner_only
async def cmd_setdailytrades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/setdailytrades <n>`")
        return
    try:
        val = int(ctx.args[0])
        await db.set_setting("max_daily_trades", val)
        await _reply(update, f"✅ Max daily trades set to *{val}*.")
    except ValueError:
        await _reply(update, "❌ Invalid value.")


@owner_only
async def cmd_setwindow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await _reply(update, "Usage: `/setwindow HH:MM HH:MM` (UTC, e.g. 09:00 17:00)")
        return
    start, end = ctx.args[0], ctx.args[1]
    await db.set_setting("trading_window_start", start)
    await db.set_setting("trading_window_end", end)
    await _reply(update, f"✅ Trading window: *{start}–{end} UTC*")


@owner_only
async def cmd_setmaxloss(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/setmaxloss <sol>` (e.g. 2.0)")
        return
    try:
        val = float(ctx.args[0])
        await db.set_setting("daily_loss_cap", val)
        await _reply(update, f"✅ Daily loss cap set to *{val} SOL*.")
    except ValueError:
        await _reply(update, "❌ Invalid value.")


@owner_only
async def cmd_setminliq(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/setminliq <usd>` (e.g. 5000)")
        return
    try:
        val = float(ctx.args[0])
        await db.set_setting("min_liquidity_usd", val)
        await _reply(update, f"✅ Minimum liquidity set to *${val:,.0f}*.")
    except ValueError:
        await _reply(update, "❌ Invalid value.")


@owner_only
async def cmd_setminage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/setminage <hours>` (e.g. 0.083 = 5 minutes)")
        return
    try:
        val = float(ctx.args[0])
        await db.set_setting("min_token_age_hours", val)
        await _reply(update, f"✅ Min token age set to *{val}h* ({val*60:.0f} min).")
    except ValueError:
        await _reply(update, "❌ Invalid value.")


@owner_only
async def cmd_autoblacklist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    on = ctx.args[0].lower() == "on" if ctx.args else True
    await db.set_setting("auto_blacklist", on)
    await _reply(update, f"✅ Auto-blacklist on stop-loss: *{'ON' if on else 'OFF'}*")


# ─────────────────────────────────────────────────────────────────────────────
# ── Token Filters ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@owner_only
async def cmd_blacklist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or len(ctx.args) < 2:
        bl = await db.get_blacklist()
        if not bl:
            await _reply(update, "Blacklist is empty.\nUse: `/blacklist add <mint>` or `/blacklist remove <mint>`")
            return
        lines = ["*🚫 Blacklist*"]
        for item in bl[:20]:
            lines.append(f"• `{item['mint'][:20]}…` — {item.get('reason','')}")
        await _reply(update, "\n".join(lines))
        return
    action = ctx.args[0].lower()
    mint = ctx.args[1]
    if action == "add":
        reason = " ".join(ctx.args[2:]) if len(ctx.args) > 2 else "Manual"
        await db.blacklist_add(mint, reason)
        await _reply(update, f"🚫 `{mint[:16]}…` added to blacklist.")
    elif action == "remove":
        await db.blacklist_remove(mint)
        await _reply(update, f"✅ `{mint[:16]}…` removed from blacklist.")
    else:
        await _reply(update, "Usage: `/blacklist add|remove <mint>`")


@owner_only
async def cmd_whitelist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or len(ctx.args) < 2:
        wl = await db.get_whitelist()
        if not wl:
            await _reply(update, "Whitelist is empty.\nUse: `/whitelist add <mint>`")
            return
        lines = ["*✅ Whitelist*"]
        for item in wl[:20]:
            lines.append(f"• `{item['mint'][:20]}…`")
        await _reply(update, "\n".join(lines))
        return
    action = ctx.args[0].lower()
    mint = ctx.args[1]
    if action == "add":
        await db.whitelist_add(mint)
        await _reply(update, f"✅ `{mint[:16]}…` added to whitelist.")
    elif action == "remove":
        await db.whitelist_remove(mint)
        await _reply(update, f"✅ `{mint[:16]}…` removed from whitelist.")


# ─────────────────────────────────────────────────────────────────────────────
# ── Manual Trading ────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@owner_only
async def cmd_trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/trade <token_mint>`")
        return
    mint = ctx.args[0]
    await _reply(update, f"🔄 Processing manual trade for `{mint[:16]}…`")
    async def notify(msg): await _reply(update, msg)
    await full_buy_flow(mint, source="manual", notify_callback=notify)


@owner_only
async def cmd_snipe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await _reply(update, "Usage: `/snipe <token_mint> <sol_amount>`")
        return
    mint, amount_str = ctx.args[0], ctx.args[1]
    try:
        amount = float(amount_str)
    except ValueError:
        await _reply(update, "❌ Invalid SOL amount.")
        return
    await _reply(update, f"🎯 Sniping `{mint[:16]}…` with *{amount} SOL*…")
    async def notify(msg): await _reply(update, msg)
    await full_buy_flow(mint, source="manual", sol_amount=amount, notify_callback=notify)


@owner_only
async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/close <token_mint | all>`")
        return
    target = ctx.args[0].lower()
    positions = await db.get_open_positions()
    if not positions:
        await _reply(update, "No open positions.")
        return
    if target == "all":
        await _reply(update, f"Closing all {len(positions)} positions…")
        for pos in positions:
            res = await execute_sell(pos, reason="manual", paper=bool(pos.get("paper")))
            await _reply(update, res["message"])
    else:
        matching = [p for p in positions if p["mint"] == target or p.get("symbol","").upper() == target.upper()]
        if not matching:
            await _reply(update, f"No position found for `{target}`.")
            return
        for pos in matching:
            res = await execute_sell(pos, reason="manual", paper=bool(pos.get("paper")))
            await _reply(update, res["message"])


@owner_only
async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/price <token_mint>`")
        return
    mint = ctx.args[0]
    await _reply(update, f"⏳ Fetching price for `{mint[:16]}…`")
    sol_price = await get_sol_price_usd()
    token_info = await get_token_info(mint)
    price_sol = await get_token_price_sol(mint)
    if price_sol:
        price_usd = price_sol * sol_price
        await _reply(
            update,
            f"*{token_info.get('symbol','???')}* Price\n"
            f"💰 {price_sol:.10f} SOL\n"
            f"💵 ${price_usd:.8f}\n"
            f"📊 SOL/USD: ${sol_price:,.2f}"
        )
    else:
        await _reply(update, f"❌ Could not get price for `{mint[:16]}…` — no liquidity route found.")


# ─────────────────────────────────────────────────────────────────────────────
# ── Reports & Analytics ───────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@owner_only
async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        date_str = ctx.args[0]
    else:
        date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    stats = await db.get_daily_report(date_str)
    if not stats:
        await _reply(update, f"No trading data for {date_str}.")
        return
    win_rate = (stats["wins"] / stats["trades"] * 100) if stats["trades"] else 0
    await _reply(
        update,
        f"*📊 Daily Report — {stats['date']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Trades: {stats['trades']}\n"
        f"Wins: {stats['wins']} | Losses: {stats['losses']}\n"
        f"Win rate: {win_rate:.1f}%\n"
        f"PnL: {stats['pnl_sol']:+.4f} SOL\n"
    )


@owner_only
async def cmd_setreporttime(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/setreporttime HH:MM` (UTC)")
        return
    await db.set_setting("report_time", ctx.args[0])
    await _reply(update, f"✅ Daily report scheduled at *{ctx.args[0]} UTC*.")


@owner_only
async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = int(ctx.args[0]) if ctx.args else config.HISTORY_DEFAULT
    history = await db.get_history(n)
    if not history:
        await _reply(update, "📭 No closed trades yet.")
        return
    lines = [f"*📜 Last {len(history)} Closed Trades*\n━━━━━━━━━━━━━━━━━━━━━"]
    for t in history:
        icon = "🟢" if t.get("pnl_sol", 0) >= 0 else "🔴"
        label = "📝" if t.get("paper") else ""
        ts = datetime.datetime.fromtimestamp(t["closed_at"], tz=datetime.timezone.utc).strftime("%m-%d %H:%M")
        lines.append(
            f"{icon}{label} *{t.get('symbol','???')}* [{t['wallet_label']}]\n"
            f"  PnL: {t.get('pnl_sol',0):+.4f} SOL ({t.get('pnl_pct',0):+.1f}%) | {ts}\n"
            f"  Reason: {t.get('reason','?')}"
        )
    await _reply(update, "\n".join(lines))


@owner_only
async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    summary = await db.get_pnl_summary()
    if not summary:
        await _reply(update, "📭 No closed trades yet.")
        return
    lines = ["*💹 All-time P&L by Wallet*\n━━━━━━━━━━━━━━━━━━━━━"]
    for wallet_label, s in summary.items():
        win_rate = (s["wins"] / s["trades"] * 100) if s["trades"] else 0
        lines.append(
            f"*{wallet_label}*\n"
            f"  Trades: {s['trades']} | WR: {win_rate:.0f}%\n"
            f"  Total PnL: {s['total_pnl']:+.4f} SOL\n"
            f"  Best: {s['best']:+.4f} | Worst: {s['worst']:+.4f}"
        )
    await _reply(update, "\n".join(lines))


@owner_only
async def cmd_clearhistory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await db.clear_history()
    await db.audit("Trade history cleared by owner", "WARN")
    await _reply(update, "🗑 Trade history and daily stats cleared.")


@owner_only
async def cmd_toptoken(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = int(ctx.args[0]) if ctx.args else 10
    tokens = await db.get_top_tokens(n)
    if not tokens:
        await _reply(update, "📭 No data yet.")
        return
    lines = [f"*🏆 Top {n} Tokens by P&L*\n━━━━━━━━━━━━━━━━━━━━━"]
    for i, t in enumerate(tokens, 1):
        icon = "🟢" if t["total_pnl"] >= 0 else "🔴"
        lines.append(
            f"{i}. {icon} *{t.get('symbol','???')}*\n"
            f"   PnL: {t['total_pnl']:+.4f} SOL | Trades: {t['trades']}"
        )
    await _reply(update, "\n".join(lines))


@owner_only
async def cmd_streak(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    streak_type, count = await db.get_streak()
    if streak_type == "none":
        await _reply(update, "📭 No trades yet.")
        return
    icon = "🔥" if streak_type == "win" else "❄️"
    await _reply(update, f"{icon} Current streak: *{count} {streak_type}s in a row*")


@owner_only
async def cmd_heatmap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = await db.get_heatmap()
    if not data:
        await _reply(update, "📭 No data for heatmap yet.")
        return
    lines = ["*🌡 Trading Heatmap (UTC hours)*\n━━━━━━━━━━━━━━━━━━━━━"]
    for hour in sorted(data.keys()):
        d = data[hour]
        bar = "█" * min(d["trades"], 10)
        pnl_icon = "🟢" if d["pnl_sol"] >= 0 else "🔴"
        lines.append(f"`{hour:02d}:00` {bar} {d['trades']}t {pnl_icon}{d['pnl_sol']:+.3f}")
    await _reply(update, "\n".join(lines))


@owner_only
async def cmd_walletpnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_pnl(update, ctx)


@owner_only
async def cmd_channelstats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Per-channel signal quality stats."""
    async with __import__("aiosqlite").connect(__import__("config").DATABASE_PATH) as dbconn:
        dbconn.row_factory = __import__("aiosqlite").Row
        async with dbconn.execute(
            """SELECT source, COUNT(*) trades,
               SUM(CASE WHEN pnl_sol>0 THEN 1 ELSE 0 END) wins,
               SUM(pnl_sol) total_pnl
               FROM trade_history WHERE paper=0 GROUP BY source"""
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await _reply(update, "📭 No data yet.")
        return
    lines = ["*📡 Channel Stats*\n━━━━━━━━━━━━━━━━━━━━━"]
    for r in rows:
        wr = (r["wins"] / r["trades"] * 100) if r["trades"] else 0
        lines.append(
            f"*{r['source']}*: {r['trades']} trades, {wr:.0f}% WR, "
            f"{r['total_pnl']:+.4f} SOL"
        )
    await _reply(update, "\n".join(lines))


@owner_only
async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_toptoken(update, ctx)


# ─────────────────────────────────────────────────────────────────────────────
# ── Price Alerts ──────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@owner_only
async def cmd_alert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await _reply(update, "Usage: `/alert <token_mint> <price_usd>`")
        return
    mint, price_str = ctx.args[0], ctx.args[1]
    try:
        price = float(price_str)
        alert_id = await db.add_alert(mint, price)
        await _reply(update, f"🔔 Alert #{alert_id} set for `{mint[:16]}…` @ ${price}")
    except ValueError:
        await _reply(update, "❌ Invalid price.")


@owner_only
async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    alerts = await db.get_alerts(triggered=False)
    if not alerts:
        await _reply(update, "📭 No active alerts.")
        return
    lines = ["*🔔 Active Price Alerts*"]
    for a in alerts:
        lines.append(f"#{a['id']} `{a['mint'][:16]}…` @ ${a['target_usd']}")
    await _reply(update, "\n".join(lines))


@owner_only
async def cmd_removealert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _reply(update, "Usage: `/removealert <token_mint>`")
        return
    await db.remove_alert(ctx.args[0])
    await _reply(update, "✅ Alert removed.")


# ─────────────────────────────────────────────────────────────────────────────
# ── Simulation / Back-test ───────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@owner_only
async def cmd_simulate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Simple back-test simulation against existing trade history."""
    history = await db.get_history(100)
    if not history:
        await _reply(update, "📭 No trade history to simulate against.")
        return
    max_loss = float(ctx.args[0]) if ctx.args else await db.get_setting("daily_loss_cap", config.DEFAULT_DAILY_LOSS_CAP)
    max_trades = int(ctx.args[1]) if len(ctx.args) > 1 else await db.get_setting("max_daily_trades", config.DEFAULT_MAX_DAILY_TRADES)

    simulated_pnl = 0.0
    trades_taken = 0
    wins = 0

    for t in history:
        if trades_taken >= max_trades:
            break
        if simulated_pnl <= -max_loss:
            break
        pnl = t.get("pnl_sol", 0)
        simulated_pnl += pnl
        trades_taken += 1
        if pnl > 0:
            wins += 1

    wr = (wins / trades_taken * 100) if trades_taken else 0
    await _reply(
        update,
        f"*🧪 Simulation Result*\n"
        f"Settings: max_loss={max_loss} SOL, max_trades={max_trades}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Trades simulated: {trades_taken}\n"
        f"Win rate: {wr:.1f}%\n"
        f"Simulated PnL: {simulated_pnl:+.4f} SOL\n"
        f"_(Based on last {len(history)} closed trades)_"
    )


# ─────────────────────────────────────────────────────────────────────────────
# ── Notifications ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

NOTIFICATION_KEYS = ["notify_buy", "notify_sell", "notify_safety_fail", "notify_scanner", "notify_alerts"]

@owner_only
async def cmd_notify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        lines = ["*🔔 Notification Settings*"]
        for key in NOTIFICATION_KEYS:
            val = await db.get_setting(key, True)
            lines.append(f"{'✅' if val else '❌'} `{key}`: {'ON' if val else 'OFF'}")
        lines.append("\nUsage: `/notify <category> on|off`")
        await _reply(update, "\n".join(lines))
        return
    if len(ctx.args) < 2:
        await _reply(update, "Usage: `/notify <category> on|off`")
        return
    key, state_str = f"notify_{ctx.args[0]}", ctx.args[1].lower()
    on = state_str == "on"
    await db.set_setting(key, on)
    await _reply(update, f"✅ `{key}` set to *{'ON' if on else 'OFF'}*")


# ─────────────────────────────────────────────────────────────────────────────
# ── Maintenance ───────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@owner_only
async def cmd_resetday(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import datetime
    date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    async with __import__("aiosqlite").connect(config.DATABASE_PATH) as dbconn:
        await dbconn.execute("DELETE FROM daily_stats WHERE date=?", (date_str,))
        await dbconn.commit()
    reset_seen_tokens()
    await db.audit("Daily counter reset by owner", "INFO")
    await _reply(update, "✅ Daily trade counter and loss tracker reset.")


@owner_only
async def cmd_scanner_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    current = await db.get_setting("scanner_enabled", False)
    new_val = not current
    await db.set_setting("scanner_enabled", new_val)
    state = "🟢 ON" if new_val else "🔴 OFF"
    await db.audit(f"Token scanner turned {state}", "INFO")
    await _reply(update, f"🔍 Token scanner: *{state}*")


@owner_only
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    help_text = """
*📖 SolBot Command Reference*
━━ Bot Control ━━
/run — Activate auto-trading
/stop — Pause auto-trading
/kill — Emergency kill switch
/revive — Re-enable after kill
/paper — Toggle paper trading mode
/pause [min] — Pause N minutes

━━ Info & Dashboard ━━
/status — Full bot status dashboard
/positions — Open trades with P&L
/settings — All current parameters
/logs — Last 20 audit log lines

━━ Wallets ━━
/balance — SOL balances of all wallets
/wallets — List wallets with status
/createwallet [label] — Create new wallet
/importwallet <key> [label] — Import wallet
/togglewallet <label> — Enable/disable wallet
/send <from> <to> <sol> — Transfer SOL
/receive [label] — Show deposit address
/exportkey <label> — Export private key

━━ Signal Channels ━━
/channels — List monitored channels
/addchannel <id> — Add channel
/removechannel <id> — Remove channel

━━ Trading Parameters ━━
/setslippage <bps> — Slippage in basis points
/setposition <pct> — Position size %
/setprofit <multiplier> — Take-profit target
/settrailing <pct> — Trailing stop %
/setcooldown <sec> — Cooldown between trades
/setdailytrades <n> — Max trades per day
/setwindow <HH:MM> <HH:MM> — Trading window (UTC)

━━ Token Filters ━━
/blacklist add|remove <token>
/whitelist add|remove <token>

━━ Trading ━━
/trade <token> — Manual trade
/snipe <token> <sol> — Snipe with custom SOL
/close <token|all> — Force-close position(s)
/price <token> — Live price info

━━ Safety Controls ━━
/setmaxloss <sol> — Daily loss limit
/setminliq <usd> — Min pool liquidity
/setminage <hours> — Min token age
/autoblacklist on|off — Auto-blacklist on stop-loss

━━ Scanner ━━
/scanner — Toggle new token scanner

━━ Reports & Analytics ━━
/report [YYYY-MM-DD] — Daily report
/history [n] — Last N closed trades
/pnl — All-time P&L summary
/toptoken [n] — Token P&L leaderboard
/streak — Win/loss streak
/heatmap — Best trading hours
/channelstats — Per-channel quality
/walletpnl — Per-wallet stats

━━ Price Alerts ━━
/alert <token> <price_usd> — Set alert
/alerts — List active alerts
/removealert <token> — Remove alert

━━ Simulation ━━
/simulate [max_loss] [max_trades]

━━ Notifications ━━
/notify — Show notification toggles
/notify <category> on|off

━━ Maintenance ━━
/resetday — Reset daily counters
/clearhistory — Wipe trade history
/setreporttime <HH:MM> — Auto-report time (UTC)
"""
    await _reply(update, help_text)
