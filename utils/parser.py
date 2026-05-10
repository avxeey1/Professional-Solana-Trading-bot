"""
utils/parser.py — Extract Solana contract addresses from arbitrary Telegram messages.
Handles: plain addresses, labeled addresses (e.g. "CA: xxx"), various formats.
"""
from __future__ import annotations
import re
from typing import Optional


# Solana base58 address pattern: 32-44 chars, base58 alphabet
_SOLANA_ADDR_RE = re.compile(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b")

# Common false-positives to skip
_SKIP_ADDRESSES = {
    "So11111111111111111111111111111111111111112",   # WSOL
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", # Token program
    "11111111111111111111111111111111",              # System program
    "SysvarRent111111111111111111111111111111111",
    "SysvarC1ock11111111111111111111111111111111",
}

# Context keywords that suggest a contract address follows
_CA_KEYWORDS = re.compile(
    r"(?:CA|contract|mint|token|address|addr|🪙|📝|🔑)\s*[:\-]?\s*",
    re.IGNORECASE,
)


def extract_solana_addresses(text: str) -> list[str]:
    """
    Extract all plausible Solana mint addresses from a message.
    Returns deduplicated list, CA-labeled addresses first.
    """
    if not text:
        return []

    found: list[str] = []
    seen: set[str] = set()

    # Priority pass: find CA: labeled addresses first
    for match in _CA_KEYWORDS.finditer(text):
        end = match.end()
        remaining = text[end:].strip()
        addr_match = _SOLANA_ADDR_RE.match(remaining)
        if addr_match:
            addr = addr_match.group(1)
            if addr not in _SKIP_ADDRESSES and addr not in seen:
                found.append(addr)
                seen.add(addr)

    # Second pass: all plausible addresses in message
    for match in _SOLANA_ADDR_RE.finditer(text):
        addr = match.group(1)
        if addr not in _SKIP_ADDRESSES and addr not in seen:
            # Basic validation: Solana addresses are typically >= 32 chars
            if 32 <= len(addr) <= 44:
                found.append(addr)
                seen.add(addr)

    return found


def extract_first_address(text: str) -> Optional[str]:
    """Return the most likely contract address from a message, or None."""
    addresses = extract_solana_addresses(text)
    return addresses[0] if addresses else None
