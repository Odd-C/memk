"""Timeline intent: reading a time range out of a natural-language question.

Separating *range* from *intent* is the whole design. A phrase like "recently"
carries a range but is usually a topical question; a phrase like "what did we do
after June" carries both a range and an explicit request for a chronology. Only the
second should switch retrieval into a date-bounded, topic-agnostic mode, because
that mode deliberately ignores topical similarity.

Parsing is deterministic and local. A date question must not depend on a model,
and a wrong range is worse than no range.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "Asia/Shanghai"

_MONTH_FROM = re.compile(
    r"(?:(?P<year>(?:19|20)\d{2})\s*年\s*)?"
    r"(?P<month>1[0-2]|0?[1-9])\s*月\s*"
    r"(?P<marker>以后|之后|以来|起|至今)"
)
_YEAR_TO_DATE = re.compile(r"(?:今年以来|今年至今|今年到现在)")
_MONTH_TO_DATE = re.compile(r"(?:本月以来|本月至今|这个月以来|这个月至今)")
_WINDOW = re.compile(
    r"(?:最近|近)\s*(?P<count>[一二两三四五六七八九十\d]{0,3})\s*"
    r"(?P<unit>天|日|周|个?月|年)"
)
_CHRONOLOGY = re.compile(
    r"(?:做了什么|干了什么|发生过什么|发生了什么|完成了什么|"
    r"有哪些(?:事情|事|变化|进展|项目|更新|活动)|"
    r"回顾|复盘|总结|时间线|历程|经历|这段时间|这期间)"
)

_CN_NUM = {
    "": 1, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
MIN_COUNT, MAX_COUNT = 1, 120


@dataclass(frozen=True)
class TimelineIntent:
    """A resolved range, and whether the question asked for a chronology."""

    start: str
    end: str | None
    kind: str
    explicit: bool

    def as_dict(self) -> dict[str, object]:
        return {"start": self.start, "end": self.end, "kind": self.kind, "explicit": self.explicit}


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or DEFAULT_TZ))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_count(text: str) -> int:
    raw = (text or "").strip()
    if raw.isdigit():
        return max(MIN_COUNT, min(MAX_COUNT, int(raw)))
    if raw in _CN_NUM:
        return _CN_NUM[raw]
    if len(raw) == 2 and raw.startswith("十") and raw[1:] in _CN_NUM:
        return min(MAX_COUNT, 10 + _CN_NUM[raw[1:]])
    if len(raw) == 2 and raw.endswith("十") and raw[:1] in _CN_NUM:
        return min(MAX_COUNT, _CN_NUM[raw[:1]] * 10)
    return 1


def _months_before(moment: datetime, months: int) -> datetime:
    """Calendar-aware month subtraction; the day is clamped to the target month."""

    index = moment.year * 12 + (moment.month - 1) - max(1, months)
    year, month_index = divmod(index, 12)
    month = month_index + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def parse(
    query: str,
    *,
    timezone_name: str = DEFAULT_TZ,
    now: datetime | None = None,
) -> TimelineIntent | None:
    """Return a timeline intent when the query states a range, else ``None``."""

    text = str(query or "").strip()
    if not text:
        return None

    zone = _zone(timezone_name)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    local = moment.astimezone(zone)
    chronology = bool(_CHRONOLOGY.search(text))

    month_match = _MONTH_FROM.search(text)
    if month_match:
        year = int(month_match.group("year") or local.year)
        month = int(month_match.group("month"))
        start = datetime(year, month, 1, tzinfo=zone)
        return TimelineIntent(_iso(start), None, "month_from", chronology)

    if _YEAR_TO_DATE.search(text):
        start = datetime(local.year, 1, 1, tzinfo=zone)
        return TimelineIntent(_iso(start), None, "year_to_date", chronology)

    if _MONTH_TO_DATE.search(text):
        start = datetime(local.year, local.month, 1, tzinfo=zone)
        return TimelineIntent(_iso(start), None, "month_to_date", chronology)

    window = _WINDOW.search(text)
    if window and chronology:
        count = parse_count(window.group("count"))
        unit = window.group("unit")
        if unit in ("天", "日"):
            start = local - timedelta(days=count)
        elif unit == "周":
            start = local - timedelta(weeks=count)
        elif unit in ("月", "个月"):
            start = _months_before(local, count)
        else:
            start = local.replace(year=local.year - count)
        return TimelineIntent(_iso(start), None, "recent_window", True)

    return None


def describe(query: str, **kwargs: object) -> dict[str, object]:
    intent = parse(query, **kwargs)  # type: ignore[arg-type]
    if intent is None:
        return {"intent": None, "range": None, "explicit": False}
    return {"intent": intent.kind, "range": intent.as_dict(), "explicit": intent.explicit}
