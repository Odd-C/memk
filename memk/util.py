"""Small shared helpers: hashing, timestamps, id generation."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timezone

_WS = re.compile(r"\s+")


def utc_now() -> str:
    """Timestamp with an explicit UTC offset, so stored times are unambiguous."""

    return datetime.now(timezone.utc).isoformat()


def now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_id(prefix: str = "m") -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def normalise_space(text: str) -> str:
    return _WS.sub(" ", (text or "").strip())


def summary_of(text: str, limit: int = 220) -> str:
    flat = normalise_space(text)
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def parse_epoch(value: str) -> int:
    """Parse a stored timestamp, tolerating both naive and offsetted forms."""

    text = (value or "").strip()
    if not text:
        return 0
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())
