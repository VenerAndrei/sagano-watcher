"""Offline tests for parsing, alert de-duplication and failure handling."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import sagano_watch as w

T0 = datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc)
STD, DIORAMA, NINNA = (a for a, _ in w.PRODUCTS)
TITLES = {"P-1": "嵯峨駅／嵐山駅　→　亀岡駅", "P-2": "亀岡駅　→　嵯峨駅／嵐山駅"}


def day(date, time, plan, qty, status="OK"):
    return {"date": date, "status": status, "allotmentQuantity": qty, "timeAvailabilities": [
        {"time": time, "status": status, "planAvailabilities": [
            {"planId": plan, "status": status, "allotmentQuantity": qty}]}]}


class Parsing(unittest.TestCase):
    def test_direction_from_title_beats_suffix(self):
        self.assertEqual(w.direction_of("X-2", "嵯峨・嵐山→亀岡"), "Saga -> Kameoka")
        self.assertEqual(w.direction_of("X-1", "亀岡→嵯峨・嵐山"), "Kameoka -> Saga")
        self.assertEqual(w.direction_of("X-1", "仁和寺拝観券＆亀岡→嵯峨・嵐山 片道乗車券"), "Kameoka -> Saga")

    def test_direction_falls_back_to_plan_suffix(self):
        self.assertEqual(w.direction_of("X-1", ""), "Saga -> Kameoka")
        self.assertEqual(w.direction_of("X-2", ""), "Kameoka -> Saga")

    def test_departure_time(self):
        self.assertEqual(w.departure_time("嵯峨野9号 13:02"), "13:02")
        self.assertEqual(w.departure_time("嵯峨野2号 9:30"), "09:30")
        self.assertEqual(w.departure_time(""), "")

    def test_hit_rules(self):
        days = [day("2026-11-25", "嵯峨野9号 13:02", "P-1", 2),
                day("2026-11-25", "嵯峨野7号 12:02", "P-1", 1),
                day("2026-11-25", "嵯峨野5号 11:02", "P-1", 5, status="NG"),
                day("2026-11-24", "嵯峨野3号 10:02", "P-1", 9)]
        hits = w.find_hits(days, TITLES, STD, "", {"2026-11-25"}, 2)
        self.assertEqual([(h.time, h.quantity) for h in hits], [("13:02", 2)])

    def test_message_format_matches_spec(self):
        hit = w.Hit(STD, "", "2026-11-25", "P-1", "Saga -> Kameoka", "13:02", 3)
        self.assertEqual(w.build_message([hit], 2),
                         "Sagano: 2 seats free — 25 Nov, Saga -> Kameoka @ 13:02 (3 left)")

    def test_untimed_hit_does_not_claim_a_train(self):
        hit = w.Hit(STD, "", "2026-11-25", "P-1", "Saga -> Kameoka", "", 3)
        message = w.build_message([hit], 2)
        self.assertIn("time not listed (3 left that day)", message)
        self.assertIn("split across trains", message)

    def test_fallback_finds_nested_days_and_plans(self):
        body = {"a": {"list": [{"date": "2026-11-25", "allotmentQuantity": 1}, {"date": "x"}]}}
        self.assertEqual(len(w.find_day_objects(body)), 1)
        self.assertEqual(w.find_plans({"x": [{"plans": [{"id": "P-1"}]}]}), [{"id": "P-1"}])

    def test_validation(self):
        with self.assertRaises(w.CheckError):
            w.validate_days([])
        with self.assertRaises(w.CheckError):
            w.validate_days([{"date": "2026-11-25", "status": "OK", "allotmentQuantity": 5,
                              "timeAvailabilities": []}])
        # Sold-out days really do look like this and must not count as broken.
        w.validate_days([{"date": "2026-11-25", "status": "NG", "allotmentQuantity": 0,
                          "timeAvailabilities": []}])


class Scenario(unittest.TestCase):
    FREE = {STD: [day("2026-11-25", "", "P-1", 3)]}
    GONE = {STD: [day("2026-11-25", "", "P-1", 0, "NG")]}

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "state.json"
        self.send = mock.Mock()
        patcher = mock.patch.object(w.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_check(self, pages, now=T0, send=None):
        """pages: activity -> day list, or an Exception to raise."""
        def fetch(activity):
            page = pages.get(activity, [day("2026-11-25", "", "Q-1", 0, "NG")])
            if isinstance(page, Exception):
                raise page
            w.validate_days(page)
            return page, TITLES
        return w.run_once({"2026-11-25"}, 2, self.path, "topic", False,
                          fetch=fetch, send=send or self.send, now=now)

    def state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_new_hit_alerts_once_then_again_after_two_hours(self):
        self.run_check(self.FREE)
        self.assertEqual(self.send.call_count, 1)
        self.run_check(self.FREE, T0 + timedelta(minutes=15))
        self.run_check(self.FREE, T0 + timedelta(minutes=119))
        self.assertEqual(self.send.call_count, 1)
        self.run_check(self.FREE, T0 + timedelta(hours=2))
        self.assertEqual(self.send.call_count, 2)

    def test_gone_then_back_alerts_immediately(self):
        self.run_check(self.FREE)
        self.run_check(self.GONE, T0 + timedelta(minutes=15))
        self.run_check(self.FREE, T0 + timedelta(minutes=30))
        self.assertEqual(self.send.call_count, 2)

    def test_all_hits_share_one_message_and_click_is_booking_page(self):
        self.run_check({STD: [day("2026-11-25", "", "P-1", 3), day("2026-11-25", "", "P-2", 4)],
                        DIORAMA: [day("2026-11-25", "嵯峨野9号 13:02", "P-1", 2)]})
        self.assertEqual(self.send.call_count, 1)
        args, kwargs = self.send.call_args
        self.assertEqual(args[1].count("\n"), 3)  # 3 hits + the day-total note
        self.assertTrue(kwargs["click"].endswith(STD))
        self.assertIn(DIORAMA, kwargs["actions"])

    def test_one_broken_alert_after_four_failures(self):
        down = {a: w.CheckError("site down") for a, _ in w.PRODUCTS}
        for _ in range(3):
            self.run_check(down)
        self.send.assert_not_called()
        for _ in range(5):
            self.run_check(down)
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("broken", self.send.call_args[0][1])

    def test_zero_days_counts_as_failure_and_recovery_rearms(self):
        for _ in range(4):
            self.run_check({STD: []})
        self.assertEqual(self.send.call_count, 1)
        self.run_check({})
        self.assertEqual(self.state()["consecutive_failures"], 0)
        self.assertFalse(self.state()["broken_alert_sent"])
        for _ in range(4):
            self.run_check({STD: []})
        self.assertEqual(self.send.call_count, 2)

    def test_failed_page_load_does_not_cause_a_duplicate_alert(self):
        self.run_check(self.FREE)
        self.run_check({STD: w.CheckError("timeout")}, T0 + timedelta(minutes=15))
        self.run_check(self.FREE, T0 + timedelta(minutes=30))
        self.assertEqual(self.send.call_count, 1)

    def test_failed_send_is_retried_next_run(self):
        code = self.run_check(self.FREE, send=mock.Mock(side_effect=OSError("offline")))
        self.assertEqual(code, 1)
        self.assertEqual(self.state()["active"], {})
        self.run_check(self.FREE, T0 + timedelta(minutes=15))
        self.assertEqual(self.send.call_count, 1)

    def test_dry_run_sends_and_saves_nothing(self):
        w.run_once({"2026-11-25"}, 2, self.path, None, True,
                   fetch=lambda a: (self.FREE.get(a, self.GONE[STD]), TITLES), send=self.send)
        self.send.assert_not_called()
        self.assertFalse(self.path.exists())


class Stop(unittest.TestCase):
    def test_stops_after_last_date_without_loading_anything(self):
        with mock.patch.object(w, "run_once") as run:
            code = w.main(["--once", "--dry-run", "--dates", "2020-01-01"])
        self.assertEqual(code, 0)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
