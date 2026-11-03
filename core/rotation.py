"""逻辑日计算与换日状态管理。"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from .config import RotationConfig

_BOUNDARY_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_DEFAULT_BOUNDARY = time(2, 0)


def parse_boundary(value: str, default: time = _DEFAULT_BOUNDARY) -> time:
    match = _BOUNDARY_RE.match(str(value or "").strip())
    if not match:
        return default
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return default
    return time(hour, minute)


def target_day(now: datetime, boundary: time) -> date:
    """返回当前时刻应当归属的日期。"""
    if now.time() >= boundary:
        return now.date()
    return now.date() - timedelta(days=1)


def resolve_day(
    now: datetime,
    *,
    boundary: time,
    last_activity: datetime | None,
    idle_minutes: int,
    current: str | None,
) -> str:
    """计算逻辑日：跨过换日点且空闲足够时才前进。"""
    target = target_day(now, boundary)
    if not current:
        return target.isoformat()
    try:
        current_date = date.fromisoformat(str(current))
    except ValueError:
        return target.isoformat()
    if current_date >= target:
        return current_date.isoformat()
    if last_activity is None or idle_minutes <= 0:
        return target.isoformat()
    if now - last_activity >= timedelta(minutes=idle_minutes):
        return target.isoformat()
    return current_date.isoformat()


class RotationManager:
    """按会话维护当前逻辑日与最后活动时间。"""

    def __init__(self, root: Path, config: RotationConfig) -> None:
        self._path = Path(root) / "state" / "rotation.json"
        self._config = config
        self._boundary = parse_boundary(config.day_boundary)
        self._sessions: dict[str, dict[str, Any]] = {}
        self._dirty = False

    @property
    def boundary(self) -> time:
        return self._boundary

    def load(self) -> None:
        self._sessions = {}
        if not self._path.is_file():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if isinstance(sessions, dict):
            for umo, entry in sessions.items():
                if isinstance(entry, dict):
                    self._sessions[str(umo)] = dict(entry)

    def snapshot(self) -> dict[str, Any]:
        return {"v": 1, "sessions": self._sessions}

    def save(self) -> None:
        if not self._dirty:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temp, self._path)
        self._dirty = False

    def day_of(self, umo: str) -> str | None:
        entry = self._sessions.get(umo)
        if not entry:
            return None
        day = entry.get("day")
        return str(day) if day else None

    def last_activity_of(self, umo: str) -> float | None:
        entry = self._sessions.get(umo)
        if not entry:
            return None
        value = entry.get("last")
        return float(value) if isinstance(value, (int, float)) else None

    def touch(self, umo: str, now: datetime) -> None:
        if not umo:
            return
        entry = self._sessions.setdefault(umo, {})
        stamp = now.timestamp()
        last = entry.get("last")
        if not isinstance(last, (int, float)) or stamp > last:
            entry["last"] = stamp
            self._dirty = True

    def resolve(self, umo: str, now: datetime, allow_advance: bool = True) -> tuple[str, bool]:
        """返回当前逻辑日，并标记是否发生换日。"""
        entry = self._sessions.setdefault(umo, {})
        current = entry.get("day")
        if not allow_advance and current:
            return str(current), False
        last = entry.get("last")
        last_dt = (
            datetime.fromtimestamp(float(last), tz=now.tzinfo)
            if isinstance(last, (int, float))
            else None
        )
        day = resolve_day(
            now,
            boundary=self._boundary,
            last_activity=last_dt,
            idle_minutes=self._config.idle_minutes,
            current=str(current) if current else None,
        )
        if day == current:
            return day, False
        entry["day"] = day
        entry.setdefault("last", now.timestamp())
        self._dirty = True
        return day, True

    def force(self, umo: str, day: str) -> None:
        if not umo:
            return
        entry = self._sessions.setdefault(umo, {})
        if entry.get("day") != day:
            entry["day"] = day
            self._dirty = True

    def tracked(self) -> list[str]:
        return list(self._sessions.keys())
