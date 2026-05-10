"""
utils/state.py — In-memory singleton for volatile bot state.
Persisted settings (slippage, take-profit, etc.) live in SQLite.
Volatile state (running, paused, kill_switch) lives here.
"""
from __future__ import annotations
import time
from typing import Any


class BotState:
    _state: dict = {
        "running": False,
        "kill_switch": False,
        "paper_mode": False,
        "paused_until": 0,
        "last_trade_time": 0,
        "start_time": time.time(),
    }

    @classmethod
    def get(cls) -> dict:
        return cls._state.copy()

    @classmethod
    def update(cls, updates: dict) -> None:
        cls._state.update(updates)

    @classmethod
    def is_running(cls) -> bool:
        return cls._state.get("running", False)

    @classmethod
    def is_killed(cls) -> bool:
        return cls._state.get("kill_switch", False)

    @classmethod
    def is_paper(cls) -> bool:
        return cls._state.get("paper_mode", False)

    @classmethod
    def is_paused(cls) -> bool:
        pt = cls._state.get("paused_until", 0)
        return time.time() < pt

    @classmethod
    def uptime_str(cls) -> str:
        secs = int(time.time() - cls._state.get("start_time", time.time()))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h {m}m {s}s"
