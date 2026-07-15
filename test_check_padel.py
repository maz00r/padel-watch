#!/usr/bin/env python3
"""Testy jednostkowe silnika (bez sieci). Uruchomienie: python3 -m unittest -v test_check_padel"""

import io
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "padel_watch"))
import check_padel as cp  # noqa: E402

TZ = ZoneInfo("Europe/Warsaw")
FILTERS = [
    {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "15:00", "end": "02:00"},
    {"days": ["sat", "sun"], "start": "00:00", "end": "24:00"},
]


def slot_at(*args):
    return {"start_utc": datetime(*args, tzinfo=TZ)}


class TestPassesFilter(unittest.TestCase):
    # 2026-07-06 to poniedziałek
    def test_weekday_afternoon_in_window(self):
        self.assertTrue(cp.passes_filter(slot_at(2026, 7, 6, 16, 0), FILTERS, TZ))

    def test_weekday_before_window(self):
        self.assertFalse(cp.passes_filter(slot_at(2026, 7, 8, 14, 0), FILTERS, TZ))

    def test_weekday_window_start_inclusive(self):
        self.assertTrue(cp.passes_filter(slot_at(2026, 7, 8, 15, 0), FILTERS, TZ))

    def test_overnight_tail_belongs_to_previous_day(self):
        # wt 01:00 = ogon poniedziałkowej nocy -> pasuje
        self.assertTrue(cp.passes_filter(slot_at(2026, 7, 7, 1, 0), FILTERS, TZ))

    def test_monday_early_morning_is_not_weekday_night(self):
        # pon 01:00 to noc niedzielna; okno pon-pt zaczyna się dopiero pon 15:00
        self.assertFalse(cp.passes_filter(slot_at(2026, 7, 6, 1, 0), FILTERS, TZ))

    def test_saturday_early_morning_matches_friday_night_or_weekend(self):
        self.assertTrue(cp.passes_filter(slot_at(2026, 7, 11, 1, 0), FILTERS, TZ))

    def test_weekend_daytime(self):
        self.assertTrue(cp.passes_filter(slot_at(2026, 7, 12, 12, 0), FILTERS, TZ))

    def test_empty_filters_pass_everything(self):
        self.assertTrue(cp.passes_filter(slot_at(2026, 7, 6, 3, 0), [], TZ))


class TestParsers(unittest.TestCase):
    def test_parse_days_range(self):
        self.assertEqual(cp.parse_days("mon-fri"), ["mon", "tue", "wed", "thu", "fri"])

    def test_parse_days_list(self):
        self.assertEqual(cp.parse_days("sat,sun"), ["sat", "sun"])

    def test_parse_days_wrapping_range(self):
        self.assertEqual(cp.parse_days("sat-mon"), ["sat", "sun", "mon"])

    def test_parse_days_invalid(self):
        with self.assertRaises(ValueError):
            cp.parse_days("xyz")

    def test_parse_filters_env(self):
        got = cp.parse_filters_env("mon-fri:15:00-02:00; sat-sun:00:00-24:00")
        self.assertEqual(got, FILTERS)

    def test_listing_id_from_url_takes_last_uuid(self):
        url = "https://go.decathlon.pl/l/slug/1c0ec93e-ca77-44b9-a3a6-c72a99d050dd"
        self.assertEqual(cp.listing_id_from_url(url), "1c0ec93e-ca77-44b9-a3a6-c72a99d050dd")

    def test_listing_id_from_url_no_uuid_raises(self):
        with self.assertRaises(ValueError):
            cp.listing_id_from_url("https://example.com/brak")

    def test_hm_to_minutes(self):
        self.assertEqual(cp.hm_to_minutes("15:30"), 930)
        self.assertEqual(cp.hm_to_minutes("24:00"), 1440)

    def test_fmt_price(self):
        self.assertEqual(cp.fmt_price(None, None), "za darmo")
        self.assertEqual(cp.fmt_price({"currency": "PLN", "amount": 0}, None), "za darmo")
        self.assertEqual(cp.fmt_price({"currency": "PLN", "amount": 1500}, None), "15.00 PLN")


class TestFreeSlots(unittest.TestCase):
    NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)

    @staticmethod
    def date_item(iso, item_id="d1", limit=1, count=0, cancelled=False, reg_end=None):
        return {
            "type": "listing-date",
            "id": item_id,
            "attributes": {
                "date": iso,
                "registrationEndDate": reg_end,
                "participantsLimit": limit,
                "participantsCount": count,
                "cancelled": cancelled,
                "name": "Rezerwacja godzinna",
                "price": None,
            },
        }

    def slots(self, *items):
        return cp.free_slots({"included": list(items)}, "L", self.NOW)

    def test_free_future_slot_included(self):
        self.assertEqual(len(self.slots(self.date_item("2026-07-07T10:00:00+00:00"))), 1)

    def test_full_slot_excluded(self):
        self.assertEqual(self.slots(self.date_item("2026-07-07T10:00:00+00:00", count=1)), [])

    def test_past_slot_excluded(self):
        self.assertEqual(self.slots(self.date_item("2026-07-05T10:00:00+00:00")), [])

    def test_cancelled_excluded(self):
        self.assertEqual(self.slots(self.date_item("2026-07-07T10:00:00+00:00", cancelled=True)), [])

    def test_no_limit_excluded(self):
        self.assertEqual(self.slots(self.date_item("2026-07-07T10:00:00+00:00", limit=None)), [])

    def test_registration_closed_excluded(self):
        item = self.date_item("2026-07-07T10:00:00+00:00", reg_end="2026-07-01T00:00:00+00:00")
        self.assertEqual(self.slots(item), [])

    def test_slot_id_is_prefixed_with_listing(self):
        (s,) = self.slots(self.date_item("2026-07-07T10:00:00+00:00", item_id="abc"))
        self.assertEqual(s["id"], "L:abc")


class TestNtfy(unittest.TestCase):
    def test_empty_topic_skips_without_network(self):
        with mock.patch.object(cp.urllib.request, "urlopen", side_effect=AssertionError("nie wolno!")):
            self.assertIsNone(cp.ntfy_post("", "t", "m"))

    def test_full_url_topic_sanitized_to_last_segment(self):
        seen = {}

        def fake_urlopen(req, timeout=30):
            seen["url"] = req.full_url
            raise urllib.error.URLError("stop")

        with mock.patch.object(cp.urllib.request, "urlopen", fake_urlopen):
            cp.ntfy_post("https://ntfy.sh/moj-temat", "t", "m")
        self.assertEqual(seen["url"], "https://ntfy.sh/moj-temat")

    def test_http_404_returns_none_no_raise(self):
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(b"{}"))
        with mock.patch.object(cp.urllib.request, "urlopen", side_effect=err):
            self.assertIsNone(cp.ntfy_post("temat", "t", "m"))


class TestNotifyRetry(unittest.TestCase):
    SLOTS = [
        {"id": f"L:{i}", "start_utc": datetime(2026, 7, 7, 10 + i, 0, tzinfo=timezone.utc),
         "name": "Rezerwacja godzinna", "price": None}
        for i in range(2)
    ]

    def test_failed_send_returns_ids_for_retry(self):
        with mock.patch.object(cp, "ntfy_post", return_value=None):
            failed = cp.notify_new("temat", list(self.SLOTS), TZ, None, "http://x")
        self.assertEqual(failed, {"L:0", "L:1"})

    def test_successful_send_returns_empty(self):
        with mock.patch.object(cp, "ntfy_post", return_value=200):
            self.assertEqual(cp.notify_new("temat", list(self.SLOTS), TZ, None, "http://x"), set())

    def test_dry_mode_returns_empty(self):
        self.assertEqual(cp.notify_new("", list(self.SLOTS), TZ, None, "http://x"), set())

    def test_batch_failure_returns_all_ids(self):
        many = [dict(s, id=f"L:{i}") for i, s in enumerate(self.SLOTS * 4)]  # 8 > 6 -> zbiorczo
        with mock.patch.object(cp, "ntfy_post", return_value=None):
            self.assertEqual(cp.notify_new("temat", many, TZ, None, "http://x"),
                             {f"L:{i}" for i in range(8)})


class TestIntervals(unittest.TestCase):
    WINDOWS = "mon-fri:15:00-02:00=30; sat-sun:08:00-22:00=60"

    def test_parse(self):
        w = cp.parse_intervals_env(self.WINDOWS)
        self.assertEqual([x["seconds"] for x in w], [30, 60])
        self.assertEqual(w[0]["days"], ["mon", "tue", "wed", "thu", "fri"])
        self.assertEqual((w[0]["start"], w[0]["end"]), ("15:00", "02:00"))

    def test_parse_clamps_minimum(self):
        w = cp.parse_intervals_env("mon-fri:10:00-12:00=1")
        self.assertEqual(w[0]["seconds"], 10)  # min 10 s

    def _at(self, *args):
        return datetime(*args, tzinfo=TZ).astimezone(timezone.utc)

    def test_inside_evening_window(self):
        w = cp.parse_intervals_env(self.WINDOWS)
        # środa 16:00 -> okno wieczorne = 30 s
        self.assertEqual(cp.current_interval(300, w, TZ, self._at(2026, 7, 8, 16, 0)), 30)

    def test_overnight_tail_uses_window(self):
        w = cp.parse_intervals_env(self.WINDOWS)
        # czwartek 01:00 = ogon środowej nocy -> 30 s
        self.assertEqual(cp.current_interval(300, w, TZ, self._at(2026, 7, 9, 1, 0)), 30)

    def test_outside_windows_uses_default(self):
        w = cp.parse_intervals_env(self.WINDOWS)
        # środa 10:00 -> poza oknami -> default
        self.assertEqual(cp.current_interval(300, w, TZ, self._at(2026, 7, 8, 10, 0)), 300)

    def test_weekend_window(self):
        w = cp.parse_intervals_env(self.WINDOWS)
        self.assertEqual(cp.current_interval(300, w, TZ, self._at(2026, 7, 11, 12, 0)), 60)

    def test_no_windows_returns_default(self):
        self.assertEqual(cp.current_interval(120, [], TZ), 120)

    def test_invalid_spec_raises(self):
        with self.assertRaises(Exception):
            cp.parse_intervals_env("zepsute-bez-sensu")


class TestStateRoundtrip(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cp, "STATE_PATH", os.path.join(td, "state.json")):
                cp.save_state({"b", "a"})
                self.assertEqual(cp.load_state(), {"a", "b"})

    def test_missing_state_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cp, "STATE_PATH", os.path.join(td, "state.json")):
                self.assertIsNone(cp.load_state())

    def test_corrupt_state_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "state.json")
            with open(p, "w") as f:
                f.write("{zepsute")
            with mock.patch.object(cp, "STATE_PATH", p):
                self.assertIsNone(cp.load_state())


if __name__ == "__main__":
    unittest.main()
