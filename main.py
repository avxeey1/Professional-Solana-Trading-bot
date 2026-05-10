"""
main.py — SolBot entry point.
Initialises the database, registers all handlers, starts background tasks,
and runs the Telegram bot via long-polling (Railway-compatible).
"""
from __future__ import annotations
import asyncio
import logging
import os
import sys
from pathlib import Path

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("solbot")

# ── Internal imports ──────────────────────────────────────────────────────────
import config
from core.database import get_db, audit, get_setting
from core.trader import monitor_positions
from core.scanner import run_scanner
from core.alert_monitor import run_alert_monitor
from core.scheduler import start_scheduler, reschedule_report
from handlers import commands as cmd
from handlers.signal_handler import handle_channel_message


# ─────────────────────────────────────────────────────────────────────────────
# Bot command menu (shown in Telegram UI)
# ─────────────────────────────────────────────────────────────────────────────
BOT_COMMANDS = [
    # Bot Control
    BotCommand("run",           "Activate auto-trading"),
    BotCommand("stop",          "Pause auto-trading"),
    BotCommand("kill",          "Emergency kill switch"),
    BotCommand("revive",        "Re-enable after kill"),
    BotCommand("paper",         "Toggle paper trading mode"),
    BotCommand("pause",         "Pause trading N minutes"),
    # Info
    BotCommand("status",        "Full bot status dashboard"),
    BotCommand("positions",     "Open trades with P&L"),
    BotCommand("settings",      "All current parameters"),
    BotCommand("logs",          "Last 20 audit log lines"),
    # Wallets
    BotCommand("balance",       "SOL balances of all wallets"),
    BotCommand("wallets",       "List wallets with status"),
    BotCommand("createwallet",  "Create new wallet"),
    BotCommand("importwallet",  "Import wallet by private key"),
    BotCommand("togglewallet",  "Enable / disable a wallet"),
    BotCommand("send",          "Transfer SOL between wallets"),
    BotCommand("receive",       "Show deposit address"),
    BotCommand("exportkey",     "Export wallet private key"),
    # Channels
    BotCommand("channels",      "List monitored signal channels"),
    BotCommand("addchannel",    "Add a signal channel"),
    BotCommand("removechannel", "Remove a signal channel"),
    # Trading params
    BotCommand("setslippage",   "Slippage in basis points"),
    BotCommand("setposition",   "Position size % of balance"),
    BotCommand("setprofit",     "Take-profit multiplier"),
    BotCommand("settrailing",   "Trailing stop %"),
    BotCommand("setcooldown",   "Cooldown seconds between trades"),
    BotCommand("setdailytrades","Max trades per day"),
    BotCommand("setwindow",     "Trading window HH:MM HH:MM UTC"),
    # Filters
    BotCommand("blacklist",     "Manage token blacklist"),
    BotCommand("whitelist",     "Manage token whitelist"),
    # Trading
    BotCommand("trade",         "Manual trade a token"),
    BotCommand("snipe",         "Snipe with custom SOL amount"),
    BotCommand("close",         "Force-close position(s)"),
    BotCommand("price",         "Live price, mcap & liquidity"),
    # Safety
    BotCommand("setmaxloss",    "Daily loss limit in SOL"),
    BotCommand("setminliq",     "Min pool liquidity USD"),
    BotCommand("setminage",     "Min token age in hours"),
    BotCommand("autoblacklist", "Auto-blacklist on stop-loss on|off"),
    # Scanner
    BotCommand("scanner",       "Toggle new token auto-scanner"),
    # Reports
    BotCommand("report",        "Daily trading report"),
    BotCommand("setreporttime", "Schedule daily report HH:MM UTC"),
    BotCommand("history",       "Last N closed trades"),
    BotCommand("pnl",           "All-time P&L summary"),
    BotCommand("clearhistory",  "Wipe full trade history"),
    BotCommand("toptoken",      "Token P&L leaderboard"),
    BotCommand("streak",        "Current win/loss streak"),
    BotCommand("heatmap",       "Best trading hours"),
    BotCommand("walletpnl",     "Per-wallet stats"),
    BotCommand("channelstats",  "Per-channel signal quality"),
    BotCommand("top",           "Today's combined leaderboard"),
    # Alerts
    BotCommand("alert",         "Set a price alert"),
    BotCommand("alerts",        "List active price alerts"),
    BotCommand("removealert",   "Remove a price alert"),
    # Simulation
    BotCommand("simulate",      "Back-test current settings"),
    # Notifications
    BotCommand("notify",        "Manage notification toggles"),
    # Maintenance
    BotCommand("resetday",      "Reset daily counters"),
    BotCommand("help",          "Full command reference"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Background task launcher
# ─────────────────────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    """Called after the application is initialised — starts background tasks."""

    # Ensure DB schema is ready
    await get_db()
    await audit("SolBot starting up…", "INFO")
    logger.info("Database initialised.")

    # Helper: send a message to the owner
    async def notify_owner(msg: str) -> None:
        try:
            await app.bot.send_message(
                chat_id=config.TELEGRAM_OWNER_ID,
                text=msg,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"notify_owner failed: {e}")

    # Register Telegram command menu
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        logger.info("Bot command menu registered.")
    except Exception as e:
        logger.warning(f"Could not set commands: {e}")

    # Start background tasks
    loop = asyncio.get_event_loop()

    loop.create_task(monitor_positions(notify_owner))
    logger.info("Position monitor started.")

    loop.create_task(run_scanner(notify_owner))
    logger.info("Token scanner started.")

    loop.create_task(run_alert_monitor(notify_owner))
    logger.info("Alert monitor started.")

    # Start scheduler (daily reports etc.)
    start_scheduler(notify_owner)
    report_time = await get_setting("report_time", "08:00")
    await reschedule_report(report_time, notify_owner)
    logger.info(f"Scheduler started. Daily report at {report_time} UTC.")

    # Greet owner
    await notify_owner(
        f"🤖 *SolBot v{config.VERSION} is online*\n"
        f"Use /status for dashboard or /help for commands.\n"
        f"⚠️ Bot is *STOPPED* by default. Send /run to activate trading."
    )
    logger.info("SolBot ready.")


# ─────────────────────────────────────────────────────────────────────────────
# Application factory
# ─────────────────────────────────────────────────────────────────────────────

def build_application() -> Application:
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    # ── Bot Control ──────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("run",            cmd.cmd_run))
    app.add_handler(CommandHandler("stop",           cmd.cmd_stop))
    app.add_handler(CommandHandler("kill",           cmd.cmd_kill))
    app.add_handler(CommandHandler("revive",         cmd.cmd_revive))
    app.add_handler(CommandHandler("paper",          cmd.cmd_paper))
    app.add_handler(CommandHandler("pause",          cmd.cmd_pause))

    # ── Info & Dashboard ─────────────────────────────────────────────────────
    app.add_handler(CommandHandler("status",         cmd.cmd_status))
    app.add_handler(CommandHandler("positions",      cmd.cmd_positions))
    app.add_handler(CommandHandler("settings",       cmd.cmd_settings))
    app.add_handler(CommandHandler("logs",           cmd.cmd_logs))

    # ── Wallets ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("balance",        cmd.cmd_balance))
    app.add_handler(CommandHandler("wallets",        cmd.cmd_wallets))
    app.add_handler(CommandHandler("createwallet",   cmd.cmd_createwallet))
    app.add_handler(CommandHandler("importwallet",   cmd.cmd_importwallet))
    app.add_handler(CommandHandler("togglewallet",   cmd.cmd_togglewallet))
    app.add_handler(CommandHandler("send",           cmd.cmd_send))
    app.add_handler(CommandHandler("receive",        cmd.cmd_receive))
    app.add_handler(CommandHandler("exportkey",      cmd.cmd_exportkey))

    # ── Signal Channels ──────────────────────────────────────────────────────
    app.add_handler(CommandHandler("channels",       cmd.cmd_channels))
    app.add_handler(CommandHandler("addchannel",     cmd.cmd_addchannel))
    app.add_handler(CommandHandler("removechannel",  cmd.cmd_removechannel))

    # ── Trading Parameters ───────────────────────────────────────────────────
    app.add_handler(CommandHandler("setslippage",    cmd.cmd_setslippage))
    app.add_handler(CommandHandler("setposition",    cmd.cmd_setposition))
    app.add_handler(CommandHandler("setprofit",      cmd.cmd_setprofit))
    app.add_handler(CommandHandler("settrailing",    cmd.cmd_settrailing))
    app.add_handler(CommandHandler("setcooldown",    cmd.cmd_setcooldown))
    app.add_handler(CommandHandler("setdailytrades", cmd.cmd_setdailytrades))
    app.add_handler(CommandHandler("setwindow",      cmd.cmd_setwindow))

    # ── Safety Controls ──────────────────────────────────────────────────────
    app.add_handler(CommandHandler("setmaxloss",     cmd.cmd_setmaxloss))
    app.add_handler(CommandHandler("setminliq",      cmd.cmd_setminliq))
    app.add_handler(CommandHandler("setminage",      cmd.cmd_setminage))
    app.add_handler(CommandHandler("autoblacklist",  cmd.cmd_autoblacklist))

    # ── Token Filters ────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("blacklist",      cmd.cmd_blacklist))
    app.add_handler(CommandHandler("whitelist",      cmd.cmd_whitelist))

    # ── Manual Trading ───────────────────────────────────────────────────────
    app.add_handler(CommandHandler("trade",          cmd.cmd_trade))
    app.add_handler(CommandHandler("snipe",          cmd.cmd_snipe))
    app.add_handler(CommandHandler("close",          cmd.cmd_close))
    app.add_handler(CommandHandler("price",          cmd.cmd_price))

    # ── Scanner ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("scanner",        cmd.cmd_scanner_toggle))

    # ── Reports & Analytics ──────────────────────────────────────────────────
    app.add_handler(CommandHandler("report",         cmd.cmd_report))
    app.add_handler(CommandHandler("setreporttime",  cmd.cmd_setreporttime))
    app.add_handler(CommandHandler("history",        cmd.cmd_history))
    app.add_handler(CommandHandler("pnl",            cmd.cmd_pnl))
    app.add_handler(CommandHandler("clearhistory",   cmd.cmd_clearhistory))
    app.add_handler(CommandHandler("toptoken",       cmd.cmd_toptoken))
    app.add_handler(CommandHandler("streak",         cmd.cmd_streak))
    app.add_handler(CommandHandler("heatmap",        cmd.cmd_heatmap))
    app.add_handler(CommandHandler("walletpnl",      cmd.cmd_walletpnl))
    app.add_handler(CommandHandler("channelstats",   cmd.cmd_channelstats))
    app.add_handler(CommandHandler("top",            cmd.cmd_top))

    # ── Price Alerts ─────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("alert",          cmd.cmd_alert))
    app.add_handler(CommandHandler("alerts",         cmd.cmd_alerts))
    app.add_handler(CommandHandler("removealert",    cmd.cmd_removealert))

    # ── Simulation ───────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("simulate",       cmd.cmd_simulate))

    # ── Notifications ────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("notify",         cmd.cmd_notify))

    # ── Maintenance ──────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("resetday",       cmd.cmd_resetday))
    app.add_handler(CommandHandler("help",           cmd.cmd_help))
    app.add_handler(CommandHandler("start",          cmd.cmd_help))

    # ── Signal channel message listener ─────────────────────────────────────
    # Catches all non-command messages from any chat (filters by channel in handler)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_channel_message,
    ))
    # Also catch channel posts forwarded into the bot's chat
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL,
        handle_channel_message,
    ))

    return app


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    # Create data directory if not present
    Path("data").mkdir(exist_ok=True)

    logger.info(f"Starting SolBot v{config.VERSION}…")

    app = build_application()

    # Run using long-polling (no webhook needed on Railway)
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,   # Skip messages received while bot was offline
        close_loop=False,
    )


if __name__ == "__main__":
    main()
