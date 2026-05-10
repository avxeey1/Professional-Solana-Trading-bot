"""
handlers/signal_handler.py — Processes incoming Telegram channel messages.
Extracts Solana contract addresses and triggers the buy flow.
"""
from __future__ import annotations

from telegram import Update, Message
from telegram.ext import ContextTypes

import config
from core import database as db
from core.trader import full_buy_flow
from utils.parser import extract_first_address, extract_solana_addresses
from utils.state import BotState


async def handle_channel_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Called for every message the bot receives.
    Checks if it originates from a monitored signal channel.
    """
    message: Message = update.message or update.channel_post
    if not message:
        return

    # Identify the source chat
    chat_id = str(message.chat.id)
    chat_username = f"@{message.chat.username}" if message.chat.username else None

    # Load monitored channels
    channels = await db.get_channels()
    monitored_ids = {ch["channel_id"] for ch in channels}

    # Check if this message comes from a monitored channel
    is_monitored = (
        chat_id in monitored_ids
        or (chat_username and chat_username in monitored_ids)
    )

    if not is_monitored:
        return  # Not from a monitored channel — ignore silently

    # Only proceed if bot is running
    state = BotState.get()
    if not state.get("running") or state.get("kill_switch"):
        return

    text = message.text or message.caption or ""
    if not text:
        await db.audit(
            f"Signal from {chat_id}: message has no text content — skipped.",
            "WARN"
        )
        return

    # Extract contract address(es)
    addresses = extract_solana_addresses(text)

    if not addresses:
        # Notify owner that a signal was received but had no address
        await db.audit(
            f"Signal from {chat_id}: no Solana contract address detected in message — skipped.\n"
            f"Message preview: {text[:120]}",
            "WARN"
        )
        # Send notification to owner
        notify_enabled = await db.get_setting("notify_safety_fail", True)
        if notify_enabled:
            try:
                await ctx.bot.send_message(
                    chat_id=config.TELEGRAM_OWNER_ID,
                    text=(
                        f"⚠️ *Signal received but no CA detected*\n"
                        f"Channel: `{chat_id}`\n"
                        f"Preview: `{text[:150]}`"
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        return

    # Use the first (most likely) address
    mint = addresses[0]

    await db.audit(f"Signal from {chat_id}: CA={mint[:16]}… — initiating trade flow", "INFO")

    # Notify owner
    notify_buy = await db.get_setting("notify_buy", True)

    async def notify_owner(msg: str) -> None:
        if notify_buy:
            try:
                await ctx.bot.send_message(
                    chat_id=config.TELEGRAM_OWNER_ID,
                    text=msg,
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    # Trigger the full buy flow
    await full_buy_flow(
        mint=mint,
        source=chat_id,
        notify_callback=notify_owner,
    )
