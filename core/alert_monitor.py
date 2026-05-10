"""
core/alert_monitor.py — Background task that monitors price alerts.
Checks active alerts every ALERT_MONITOR_INTERVAL_SEC and notifies owner.
"""
from __future__ import annotations
import asyncio

import config
from core import database as db
from core.jupiter import get_token_price_sol, get_sol_price_usd, get_token_info
from utils.state import BotState


async def run_alert_monitor(notify_callback) -> None:
    """
    Background loop — checks price alerts and fires notifications.
    notify_callback(msg: str) sends a message to the owner.
    """
    while True:
        try:
            alerts = await db.get_alerts(triggered=False)
            if alerts:
                sol_price = await get_sol_price_usd()
                for alert in alerts:
                    mint = alert["mint"]
                    target_usd = alert["target_usd"]
                    price_sol = await get_token_price_sol(mint)
                    if price_sol is None:
                        continue
                    current_usd = price_sol * sol_price
                    if current_usd >= target_usd:
                        token_info = await get_token_info(mint)
                        symbol = token_info.get("symbol", mint[:8])
                        await db.mark_alert_triggered(alert["id"])
                        notify_enabled = await db.get_setting("notify_alerts", True)
                        if notify_enabled:
                            await notify_callback(
                                f"🔔 *Price Alert Triggered!*\n"
                                f"Token: *{symbol}*\n"
                                f"`{mint}`\n"
                                f"Target: ${target_usd:.6f}\n"
                                f"Current: ${current_usd:.6f}\n"
                                f"SOL price: ${sol_price:,.2f}"
                            )
                        await db.audit(
                            f"Price alert triggered: {symbol} @ ${current_usd:.6f} (target ${target_usd:.6f})",
                            "INFO"
                        )
        except Exception as e:
            await db.audit(f"Alert monitor error: {e}", "ERROR")

        await asyncio.sleep(config.ALERT_MONITOR_INTERVAL_SEC)
