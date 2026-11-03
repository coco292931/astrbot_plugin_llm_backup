"""逻辑日与换日规则测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path

from core.config import RotationConfig
from core.rotation import RotationManager, parse_boundary, resolve_day, target_day


class BoundaryTests(unittest.TestCase):
    def test_parse_valid(self) -> None:
        self.assertEqual(parse_boundary("02:30"), time(2, 30))
        self.assertEqual(parse_boundary("23:59"), time(23, 59))

    def test_parse_invalid_falls_back(self) -> None:
        self.assertEqual(parse_boundary(""), time(2, 0))
        self.assertEqual(parse_boundary("25:00"), time(2, 0))
        self.assertEqual(parse_boundary("2点半"), time(2, 0))


class TargetDayTests(unittest.TestCase):
    def test_before_boundary_counts_as_previous_day(self) -> None:
        now = datetime(2026, 11, 4, 1, 30)
        self.assertEqual(target_day(now, time(2, 0)).isoformat(), "2026-11-03")

    def test_after_boundary_counts_as_today(self) -> None:
        now = datetime(2026, 11, 4, 2, 0)
        self.assertEqual(target_day(now, time(2, 0)).isoformat(), "2026-11-04")


class ResolveDayTests(unittest.TestCase):
    boundary = time(2, 0)

    def test_first_use_uses_target(self) -> None:
        now = datetime(2026, 11, 4, 1, 0)
        self.assertEqual(
            resolve_day(
                now,
                boundary=self.boundary,
                last_activity=None,
                idle_minutes=120,
                current=None,
            ),
            "2026-11-03",
        )

    def test_keeps_current_while_active_after_boundary(self) -> None:
        now = datetime(2026, 11, 4, 3, 0)
        self.assertEqual(
            resolve_day(
                now,
                boundary=self.boundary,
                last_activity=now - timedelta(minutes=10),
                idle_minutes=120,
                current="2026-11-03",
            ),
            "2026-11-03",
        )

    def test_advances_after_idle(self) -> None:
        now = datetime(2026, 11, 4, 5, 0)
        self.assertEqual(
            resolve_day(
                now,
                boundary=self.boundary,
                last_activity=now - timedelta(minutes=121),
                idle_minutes=120,
                current="2026-11-03",
            ),
            "2026-11-04",
        )

    def test_zero_idle_advances_immediately(self) -> None:
        now = datetime(2026, 11, 4, 2, 1)
        self.assertEqual(
            resolve_day(
                now,
                boundary=self.boundary,
                last_activity=now,
                idle_minutes=0,
                current="2026-11-03",
            ),
            "2026-11-04",
        )

    def test_never_moves_backwards(self) -> None:
        now = datetime(2026, 11, 4, 1, 0)
        self.assertEqual(
            resolve_day(
                now,
                boundary=self.boundary,
                last_activity=now,
                idle_minutes=120,
                current="2026-11-05",
            ),
            "2026-11-05",
        )

    def test_gap_of_several_days_jumps_to_target(self) -> None:
        now = datetime(2026, 11, 10, 9, 0)
        self.assertEqual(
            resolve_day(
                now,
                boundary=self.boundary,
                last_activity=datetime(2026, 11, 1, 9, 0),
                idle_minutes=120,
                current="2026-11-01",
            ),
            "2026-11-10",
        )


class RotationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.config = RotationConfig(day_boundary="02:00", idle_minutes=120)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_touch_and_resolve_persist(self) -> None:
        manager = RotationManager(self.root, self.config)
        umo = "aiocqhttp:FriendMessage:10001"
        now = datetime(2026, 11, 4, 10, 0)
        manager.touch(umo, now)
        day, changed = manager.resolve(umo, now)
        self.assertEqual(day, "2026-11-04")
        self.assertTrue(changed)
        manager.save()

        reloaded = RotationManager(self.root, self.config)
        reloaded.load()
        self.assertEqual(reloaded.day_of(umo), "2026-11-04")
        self.assertIsNotNone(reloaded.last_activity_of(umo))

    def test_force_sets_day(self) -> None:
        manager = RotationManager(self.root, self.config)
        umo = "aiocqhttp:FriendMessage:10001"
        manager.force(umo, "2026-12-01")
        self.assertEqual(manager.day_of(umo), "2026-12-01")

    def test_disallow_advance_keeps_current_day(self) -> None:
        manager = RotationManager(self.root, self.config)
        umo = "aiocqhttp:FriendMessage:10001"
        manager.force(umo, "2026-11-03")
        now = datetime(2026, 11, 6, 12, 0)
        day, changed = manager.resolve(umo, now, allow_advance=False)
        self.assertEqual(day, "2026-11-03")
        self.assertFalse(changed)

    def test_disallow_advance_still_bootstraps(self) -> None:
        manager = RotationManager(self.root, self.config)
        day, changed = manager.resolve(
            "aiocqhttp:FriendMessage:1",
            datetime(2026, 11, 6, 12, 0),
            allow_advance=False,
        )
        self.assertEqual(day, "2026-11-06")
        self.assertTrue(changed)


if __name__ == "__main__":
    unittest.main()
