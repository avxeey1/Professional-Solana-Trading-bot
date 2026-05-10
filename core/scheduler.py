"""
core/scheduler.py — APScheduler-based scheduled jobs.
Daily report, periodic health checks.
"""
from __future__ import annotations
import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from core import database as db
from core.jupiter import get_sol_price_usd
from core.wallet import get_all_balances


_scheduler: AsyncIOScheduler | None = None


async def _send_daily_report(notify_callback) -> None:
    date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    stats = await db.get_daily_report(date_str)
    sol_price = await get_sol_price_usd()
    balances = await get_all_balances()
    total_sol = sum(w["balance_sol"] for w in balances if w["balance_sol"] >= 0)

    if not stats:
        await notify_callback(
            f"📊 *Daily Report — {date_str}*\nNo trades recorded today."
        )
        return

    trades = stats.get("trades", 0)
    wins   = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    pnl    = stats.get("pnl_sol", 0.0)
    win_rate = (wins / trades * 100) if trades else 0

    await notify_callback(
        f"📊 *Daily Report — {date_str}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Trades : {trades}\n"
        f"Wins   : {wins} | Losses: {losses}\n"
        f"Win rate: {win_rate:.1f}%\n"
        f"PnL    : {pnl:+.4f} SOL (≈${pnl*sol_price:+,.2f})\n"
        f"Balance: {total_sol:.4f} SOL (≈${total_sol*sol_price:,.2f})\n"
        f"SOL/USD: ${sol_price:,.2f}"
    )


def start_scheduler(notify_callback) -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="UTC")

    async def _report_job():
        await _send_daily_report(notify_callback)

    # Default report time — overridden dynamically via /setreporttime
    _scheduler.add_job(
        _report_job,
        CronTrigger(hour=8, minute=0, timezone="UTC"),
        id="daily_report",
        replace_existing=True,
    )

    _scheduler.start()


async def reschedule_report(time_str: str, notify_callback) -> None:
    """Reschedule the daily report job (called after /setreporttime)."""
    global _scheduler
    if not _scheduler:
        return
    try:
        hh, mm = map(int, time_str.split(":"))
    except ValueError:
        return

    async def _report_job():
        await _send_daily_report(notify_callback)

    _scheduler.add_job(
        _report_job,
        CronTrigger(hour=hh, minute=mm, timezone="UTC"),
        id="daily_report",
        replace_existing=True,
    )
    await db.audit(f"Daily report rescheduled to {time_str} UTC", "INFO")
