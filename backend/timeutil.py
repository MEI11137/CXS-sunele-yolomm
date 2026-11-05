"""时间口径。

库里统一存 UTC ISO（``2026-11-05T00:10:45Z``），便于排序与跨机器一致；
但用户和界面说的是**本地日期**。两者的日界差 8 小时，最容易出现的情况是：

* 界面上“今天”的最后几条记录，按 UTC 日期算会落到“昨天/明天”；
* 于是 Agent 用 UTC 日期去查，会报告“没有记录”，而用户明明看得见。

所以对外一律**同时**给：``created_at``（UTC，机器用）、``created_at_local``（带偏移，
人看的），查询参数 ``date_from/date_to`` 按**本地日期**解释，返回里明确写出换算后的
UTC 边界（``applied``），避免各说各话。
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone

DEFAULT_OFFSET_MINUTES = 480  # UTC+8


def offset_minutes() -> int:
    raw = (os.environ.get("HX17_UTC_OFFSET_MINUTES") or "").strip()
    if not raw:
        return DEFAULT_OFFSET_MINUTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_OFFSET_MINUTES
    return max(-12 * 60, min(14 * 60, value))


def local_zone() -> timezone:
    return timezone(timedelta(minutes=offset_minutes()))


def to_local(iso_utc: str | None) -> str | None:
    if not iso_utc:
        return None
    text = str(iso_utc).strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return text
    return parsed.astimezone(local_zone()).isoformat(timespec="seconds")


def day_bounds(date_from: str | None, date_to: str | None) -> dict:
    """把本地日期（含端点）换算成 UTC ISO 区间，供 SQL 字符串比较。"""
    zone = local_zone()
    start = _parse_day(date_from, fallback_offset_days=-30)
    end = _parse_day(date_to, fallback_offset_days=0)
    if start > end:
        start, end = end, start
    start_utc = datetime.combine(start, time.min, tzinfo=zone).astimezone(timezone.utc)
    end_utc = datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
    return {
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "utc_from": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "utc_to_exclusive": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "offset_minutes": offset_minutes(),
    }


def _parse_day(raw: str | None, *, fallback_offset_days: int) -> date:
    text = str(raw or "").strip()
    if text:
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            pass
    return (datetime.now(local_zone()) + timedelta(days=fallback_offset_days)).date()


def today_local() -> str:
    return datetime.now(local_zone()).date().isoformat()


__all__ = ["day_bounds", "local_zone", "offset_minutes", "to_local", "today_local"]
