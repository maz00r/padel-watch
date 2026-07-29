#!/usr/bin/env python3
"""Testy monitora Cinema City (bez sieci). Uruchomienie: python3 -m unittest -v test_check_cinema"""

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "cinema_watch"))
import check_cinema as cc  # noqa: E402

FILM_URL = ("https://www.cinema-city.pl/filmy/odyseja/7460s2r#/buy-tickets-by-film"
            "?in-cinema=warszawa&at=2026-08-04&for-movie=7460s2r&view-mode=list")


def event(eid, when, cinema_id="1074", film="7460s2r"):
    return {"id": eid, "filmId": film, "cinemaId": cinema_id,
            "businessDay": when[:10], "eventDateTime": when,
            "bookingLink": f"https://tickets.cinema-city.pl/api/order/{eid}?lang=pl"}


CINEMAS = [{"id": "1074", "displayName": "Warszawa -  Arkadia"},
           {"id": "1067", "displayName": "Warszawa - Mokotów"}]


class ParseFilmUrlTest(unittest.TestCase):
    def test_full_url_from_address_bar(self):
        film_id, place, slug = cc.parse_film_url(FILM_URL)
        self.assertEqual((film_id, place, slug), ("7460s2r", "warszawa", "odyseja"))

    def test_id_taken_from_path_when_query_lacks_it(self):
        film_id, place, _ = cc.parse_film_url(
            "https://www.cinema-city.pl/filmy/odyseja/7460s2r#/x?in-cinema=warszawa")
        self.assertEqual((film_id, place), ("7460s2r", "warszawa"))

    def test_numeric_place_is_single_cinema(self):
        with mock.patch.dict(os.environ, {"FILM_URL": FILM_URL.replace("warszawa", "1074")}):
            self.assertFalse(cc.load_config()["is_group"])

    def test_group_place_is_group(self):
        with mock.patch.dict(os.environ, {"FILM_URL": FILM_URL}):
            self.assertTrue(cc.load_config()["is_group"])

    def test_missing_cinema_explains_how_to_get_link(self):
        with self.assertRaises(ValueError) as ctx:
            cc.parse_film_url("https://www.cinema-city.pl/filmy/odyseja/7460s2r")
        self.assertIn("in-cinema", str(ctx.exception))

    def test_missing_film_id(self):
        with self.assertRaises(ValueError):
            cc.parse_film_url("https://www.cinema-city.pl/cos?in-cinema=warszawa")

    def test_empty_url(self):
        with self.assertRaises(ValueError):
            cc.parse_film_url("")


class NormalizeEventTest(unittest.TestCase):
    def test_strips_city_prefix_from_cinema_name(self):
        names = {c["id"]: c["displayName"] for c in CINEMAS}
        got = cc.normalize_event(event("1", "2026-08-04T21:15:00"), names)
        self.assertEqual(got["cinema"], "Arkadia")
        self.assertEqual((got["date"], got["time"]), ("2026-08-04", "21:15"))

    def test_unknown_cinema_falls_back_to_id(self):
        got = cc.normalize_event(event("1", "2026-08-04T10:00:00", cinema_id="9999"), {})
        self.assertEqual(got["cinema"], "9999")


class PluralTest(unittest.TestCase):
    def test_screenings(self):
        got = [cc.screenings(n) for n in (1, 2, 4, 5, 12, 13, 14, 22, 25)]
        self.assertEqual(got, ["1 seans", "2 seanse", "4 seanse", "5 seansów", "12 seansów",
                               "13 seansów", "14 seansów", "22 seanse", "25 seansów"])

    def test_day_count(self):
        self.assertEqual([cc.day_count(n) for n in (1, 2, 5)],
                         ["1 dniu", "2 dniach", "5 dniach"])

    def test_zero_uses_many_form(self):
        self.assertEqual(cc.screenings(0), "0 seansów")


class FmtDayTest(unittest.TestCase):
    def test_polish_weekday(self):
        self.assertEqual(cc.fmt_day("2026-08-07"), "piątek 07.08")
        self.assertEqual(cc.fmt_day("2026-08-07", short=True), "pt 07.08")

    def test_garbage_passes_through(self):
        self.assertEqual(cc.fmt_day("nie-data"), "nie-data")


class DescribeNewTest(unittest.TestCase):
    def events(self, *specs):
        names = {c["id"]: c["displayName"] for c in CINEMAS}
        return [cc.normalize_event(event(str(i), when), names) for i, when in enumerate(specs)]

    def test_new_day_is_summarised(self):
        new = self.events("2026-08-07T10:00:00", "2026-08-07T21:45:00")
        text = cc.describe_new(new, {"2026-08-07"})
        self.assertIn("🆕 piątek 07.08 — 2 seanse, 10:00–21:45", text)

    def test_single_showing_uses_singular(self):
        text = cc.describe_new(self.events("2026-08-07T10:00:00"), {"2026-08-07"})
        self.assertIn("1 seans,", text)

    def test_addition_to_known_day_lists_times(self):
        new = self.events("2026-08-04T22:30:00")
        text = cc.describe_new(new, set())
        self.assertIn("➕ wt 04.08: 22:30 Arkadia", text)

    def test_long_addition_is_truncated(self):
        new = self.events(*[f"2026-08-04T1{i}:00:00" for i in range(8)])
        text = cc.describe_new(new, set())
        self.assertIn("(+2)", text)   # 8 seansów: 6 pokazanych + 2 ukryte

    def test_new_day_and_addition_together(self):
        new = self.events("2026-08-07T10:00:00", "2026-08-04T22:30:00")
        text = cc.describe_new(new, {"2026-08-07"})
        self.assertIn("🆕 piątek 07.08", text)
        self.assertIn("➕ wt 04.08", text)


class CollectEventsTest(unittest.TestCase):
    def setUp(self):
        with mock.patch.dict(os.environ, {"FILM_URL": FILM_URL}, clear=False):
            self.cfg = cc.load_config()

    def fake_api(self, dates, events_by_date):
        def get(url):
            if "/dates/" in url:
                return {"body": {"dates": dates}}
            day = url.split("/at-date/")[1].split("?")[0]
            return {"body": {"cinemas": CINEMAS, "events": events_by_date.get(day, [])}}
        return get

    def test_collects_and_derives_dates(self):
        api = self.fake_api(["2026-08-04", "2026-08-05"], {
            "2026-08-04": [event("1", "2026-08-04T10:00:00")],
            "2026-08-05": [],
        })
        with mock.patch.object(cc, "http_get_json", side_effect=api):
            events, dates = cc.collect_events(self.cfg)
        self.assertEqual(len(events), 1)
        self.assertEqual(dates, ["2026-08-04"])  # dzień bez seansu nie liczy się jako dzień

    def test_cinema_filter_matches_substring_case_insensitively(self):
        self.cfg["cinemas"] = ["mokotów"]
        api = self.fake_api(["2026-08-04"], {"2026-08-04": [
            event("1", "2026-08-04T10:00:00", cinema_id="1074"),
            event("2", "2026-08-04T11:00:00", cinema_id="1067"),
        ]})
        with mock.patch.object(cc, "http_get_json", side_effect=api):
            events, _ = cc.collect_events(self.cfg)
        self.assertEqual([e["id"] for e in events], ["2"])

    def test_single_cinema_mode_drops_other_films(self):
        self.cfg["is_group"] = False
        api = self.fake_api(["2026-08-04"], {"2026-08-04": [
            event("1", "2026-08-04T10:00:00"),
            event("2", "2026-08-04T11:00:00", film="inny-film"),
        ]})
        with mock.patch.object(cc, "http_get_json", side_effect=api):
            events, _ = cc.collect_events(self.cfg)
        self.assertEqual([e["id"] for e in events], ["1"])


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.object(cc, "STATE_PATH", os.path.join(self.dir.name, "s.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = mock.patch.dict(os.environ, {"FILM_URL": FILM_URL, "NTFY_TOPIC": "t",
                                                "CINEMAS": "", "DAYS_AHEAD": "365"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sent = []

    def run_with(self, events, ntfy_ok=True):
        def api(url):
            days = sorted({e["businessDay"] for e in events})
            if "/dates/" in url:
                return {"body": {"dates": days}}
            day = url.split("/at-date/")[1].split("?")[0]
            return {"body": {"cinemas": CINEMAS,
                             "events": [e for e in events if e["businessDay"] == day]}}

        def ntfy(topic, title, message, click=None, tags="clapper"):
            self.sent.append((title, message))
            return ntfy_ok

        with mock.patch.object(cc, "http_get_json", side_effect=api), \
                mock.patch.object(cc, "ntfy_post", side_effect=ntfy):
            return cc.run_once()

    def state(self):
        with open(cc.STATE_PATH, encoding="utf-8") as f:
            return json.load(f)

    def test_first_run_baselines_without_alert(self):
        rc = self.run_with([event("1", "2026-08-04T10:00:00")])
        self.assertEqual(rc, 0)
        self.assertEqual(self.sent, [])          # announce_startup=False w tym wywołaniu
        self.assertEqual(self.state()["event_ids"], ["1"])

    def test_second_run_without_changes_is_quiet(self):
        self.run_with([event("1", "2026-08-04T10:00:00")])
        rc = self.run_with([event("1", "2026-08-04T10:00:00")])
        self.assertEqual((rc, self.sent), (0, []))

    def test_new_showing_notifies_and_is_remembered(self):
        self.run_with([event("1", "2026-08-04T10:00:00")])
        rc = self.run_with([event("1", "2026-08-04T10:00:00"),
                            event("2", "2026-08-07T21:00:00")])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("🆕 piątek 07.08", self.sent[0][1])
        self.assertEqual(self.state()["event_ids"], ["1", "2"])
        self.sent.clear()
        self.assertEqual(self.run_with([event("1", "2026-08-04T10:00:00"),
                                        event("2", "2026-08-07T21:00:00")]), 0)
        self.assertEqual(self.sent, [])          # drugi raz o tym samym nie powiadamiamy

    def test_failed_push_keeps_state_so_alert_is_retried(self):
        self.run_with([event("1", "2026-08-04T10:00:00")])
        before = self.state()
        rc = self.run_with([event("1", "2026-08-04T10:00:00"),
                            event("2", "2026-08-07T21:00:00")], ntfy_ok=False)
        self.assertEqual(rc, 2)
        self.assertEqual(self.state(), before)   # stan nietknięty -> ponowimy
        self.sent.clear()
        self.assertEqual(self.run_with([event("1", "2026-08-04T10:00:00"),
                                        event("2", "2026-08-07T21:00:00")]), 0)
        self.assertEqual(len(self.sent), 1)      # ponowiony push doszedł

    def test_network_error_leaves_state_untouched(self):
        self.run_with([event("1", "2026-08-04T10:00:00")])
        before = self.state()
        boom = urllib.error.URLError("padlo")
        with mock.patch.object(cc, "http_get_json", side_effect=boom), \
                mock.patch.object(cc, "ntfy_post", side_effect=AssertionError("nie wolno")):
            rc = cc.run_once()
        self.assertEqual(rc, 2)
        self.assertEqual(self.state(), before)

    def test_empty_repertoire_warns_about_bad_link(self):
        with mock.patch.object(cc, "log") as logged:
            self.run_with([])
        said = " ".join(str(c) for c in logged.call_args_list)
        self.assertIn("Sprawdź film_url", said)

    def test_broken_config_reports_and_does_not_crash(self):
        with mock.patch.dict(os.environ, {"FILM_URL": "nonsens"}):
            self.assertEqual(cc.run_once(), 1)

    def test_past_days_are_pruned_from_state(self):
        self.run_with([event("1", "2026-08-04T10:00:00"), event("2", "2026-08-05T10:00:00")])
        self.run_with([event("2", "2026-08-05T10:00:00")])   # 04.08 minął, API go nie zwraca
        self.assertEqual(self.state()["event_ids"], ["2"])
        self.assertEqual(self.state()["dates"], ["2026-08-05"])


class StateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "s.json")
        patcher = mock.patch.object(cc, "STATE_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_state_is_first_run(self):
        self.assertIsNone(cc.load_state())

    def test_corrupt_state_is_first_run(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{niepoprawny json")
        self.assertIsNone(cc.load_state())

    def test_save_and_load_roundtrip(self):
        cc.save_state({"b", "a"}, {"2026-08-04"})
        self.assertEqual(cc.load_state(), {"event_ids": ["a", "b"], "dates": ["2026-08-04"]})

    def test_clear_state_removes_file(self):
        cc.save_state({"a"}, {"2026-08-04"})
        with mock.patch.dict(os.environ, {"CLEAR_STATE": "all"}):
            cc.apply_clear_state()
        self.assertIsNone(cc.load_state())

    def test_clear_state_ignored_when_empty(self):
        cc.save_state({"a"}, {"2026-08-04"})
        with mock.patch.dict(os.environ, {"CLEAR_STATE": ""}):
            cc.apply_clear_state()
        self.assertIsNotNone(cc.load_state())


if __name__ == "__main__":
    unittest.main()
