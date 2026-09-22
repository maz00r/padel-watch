#!/usr/bin/env python3
"""Testy Watykan Watch, bez sieci i bez prawdziwego ntfy."""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vatican_watch"))
import check_vatican as vw  # noqa: E402


TARGET = "Musei Vaticani - Biglietti d'ingresso"


def product(status="SOLD_OUT", **changes):
    base = {"id": 123, "name": TARGET, "suggestion": "", "availability": status}
    base.update(changes)
    return base


class ConfigTest(unittest.TestCase):
    def test_defaults_match_deployment_scope(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = vw.load_config()
        self.assertEqual((cfg["start_date"], cfg["end_date"], cfg["visitors"], cfg["interval"]),
                         (date(2026, 9, 24), date(2026, 9, 28), 5, 60))

    def test_rejects_invalid_range_and_interval(self):
        with mock.patch.dict(os.environ, {"START_DATE": "2026-09-29", "END_DATE": "2026-09-28"}, clear=True):
            with self.assertRaisesRegex(ValueError, "end_date"):
                vw.load_config()
        with mock.patch.dict(os.environ, {"CHECK_INTERVAL": "29"}, clear=True):
            with self.assertRaisesRegex(ValueError, "30"):
                vw.load_config()

    def test_active_dates_omit_days_past_in_rome(self):
        cfg = {"start_date": date(2026, 9, 24), "end_date": date(2026, 9, 28)}
        self.assertEqual(vw.active_dates(cfg, date(2026, 9, 26)),
                         [date(2026, 9, 26), date(2026, 9, 27), date(2026, 9, 28)])
        self.assertEqual(vw.active_dates(cfg, date(2026, 9, 29)), [])


class ProductSelectionTest(unittest.TestCase):
    def test_exact_product_is_selected_even_when_id_changes(self):
        first = product("SOLD_OUT", id=1653513808)
        second = product("LOW_AVAILABILITY", id=903348722)
        self.assertEqual(vw.select_target_product({"visits": [first]})["id"], 1653513808)
        self.assertEqual(vw.select_target_product({"visits": [second]})["availability"], "LOW_AVAILABILITY")

    def test_available_suggestion_for_another_object_is_ignored(self):
        suggestion = product("AVAILABLE", id=99, name="Palazzo Papale - Biglietti d'ingresso",
                             suggestion="Ti suggeriamo anche:")
        chosen = vw.select_target_product({"visits": [suggestion, product()]})
        self.assertEqual(chosen["name"], TARGET)

    def test_guided_school_and_pilgrimage_products_are_ignored(self):
        variants = [
            product("AVAILABLE", name="Musei Vaticani - Visite Guidate Singoli Musei"),
            product("AVAILABLE", name="Musei Vaticani - Didattiche - Biglietti d'ingresso"),
            product("AVAILABLE", name="Musei Vaticani - Pellegrinaggi - Biglietti d'ingresso"),
        ]
        self.assertIsNone(vw.select_target_product({"visits": variants}))

    def test_missing_target_is_missing_not_sold_out(self):
        with mock.patch.object(vw, "http_get_json", return_value={"visits": []}):
            status, product_id = vw.fetch_status(date(2026, 9, 27))
        self.assertEqual((status, product_id), ("MISSING", ""))

    def test_target_with_suggestion_is_ignored_too(self):
        self.assertIsNone(vw.select_target_product({"visits": [
            product("AVAILABLE", suggestion="Ti suggeriamo anche:")]}))

    def test_malformed_payload_is_contract_error(self):
        with self.assertRaisesRegex(vw.VaticanError, "visits"):
            vw.select_target_product({"oops": []})

    def test_unknown_availability_is_missing(self):
        self.assertEqual(vw.status_from_product(product("SOMETHING_NEW")), "MISSING")


class RetryTest(unittest.TestCase):
    def test_result_url_is_official_angular_results_route_in_rome_time(self):
        self.assertEqual(vw.result_url(date(2026, 9, 24)),
                         "https://tickets.museivaticani.va/home/visit/5/1790200800000/1/")

    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(vw.parse_retry_after("42"), 42)
        then = datetime(2026, 9, 24, 12, 1, tzinfo=timezone.utc)
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(vw.parse_retry_after("Thu, 24 Sep 2026 12:01:00 GMT", now), 60)

    def test_exponential_delay_honours_retry_after_and_has_cap(self):
        self.assertEqual(vw.retry_delay(60, 1), 60)
        self.assertEqual(vw.retry_delay(60, 2), 120)
        self.assertEqual(vw.retry_delay(60, 2, 900), 900)
        self.assertEqual(vw.retry_delay(60, 99, 99999), vw.MAX_BACKOFF_SECONDS)


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "vatican_state.json")
        patcher = mock.patch.object(vw, "STATE_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_atomic_roundtrip(self):
        state = vw.default_state()
        state["last_observed"] = {"2026-09-24": "SOLD_OUT"}
        vw.save_state(state)
        self.assertEqual(vw.load_state()["last_observed"], {"2026-09-24": "SOLD_OUT"})
        self.assertEqual([name for name in os.listdir(self.tmp.name) if name.endswith(".tmp")], [])

    def test_corrupt_state_does_not_stop_monitor(self):
        with open(self.path, "w", encoding="utf-8") as state_file:
            state_file.write("not-json")
        self.assertEqual(vw.load_state(), vw.default_state())

    def test_malformed_failure_counter_is_reset(self):
        with open(self.path, "w", encoding="utf-8") as state_file:
            json.dump({"failure": {"count": "not-a-number", "alerted": True}}, state_file)
        self.assertEqual(vw.load_state()["failure"], {"count": 0, "alerted": True})


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "vatican_state.json")
        self.state_patch = mock.patch.object(vw, "STATE_PATH", self.path)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.env = mock.patch.dict(os.environ, {
            "START_DATE": "2026-09-24", "END_DATE": "2026-09-28", "CHECK_INTERVAL": "60",
            "NTFY_TOPIC": "unit-test-topic",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sent = []

    def run_with(self, statuses, ntfy_ok=True, today=date(2026, 9, 24), announce=False):
        def fetch(day, _visitors):
            value = statuses[day.isoformat()]
            if isinstance(value, Exception):
                raise value
            return value, "id-" + day.isoformat()

        def notify(*args, **kwargs):
            self.sent.append((args, kwargs))
            return ntfy_ok

        with mock.patch.object(vw, "fetch_status", side_effect=fetch), \
                mock.patch.object(vw, "ntfy_post", side_effect=notify), \
                mock.patch.object(vw.time, "sleep"):
            return vw.run_once(announce_startup=announce, today=today)

    def statuses(self, value):
        return {f"2026-09-{day:02d}": value for day in range(24, 29)}

    def state(self):
        return vw.load_state()

    def test_first_available_run_alerts_at_high_priority(self):
        rc, _ = self.run_with(self.statuses("AVAILABLE"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1)
        args, kwargs = self.sent[0]
        self.assertIn("bilety dostępne", args[1])
        self.assertEqual(kwargs["priority"], "high")
        self.assertEqual(kwargs["click"],
                         "https://tickets.museivaticani.va/home/visit/5/1790200800000/1/")
        self.assertEqual(set(self.state()["last_notified"]), set(self.statuses("x")))

    def test_sold_out_is_quiet_and_second_identical_run_is_quiet(self):
        self.assertEqual(self.run_with(self.statuses("SOLD_OUT"))[0], 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.run_with(self.statuses("SOLD_OUT"))[0], 0)
        self.assertEqual(self.sent, [])

    def test_availability_is_not_repeated_until_it_is_sold_out_again(self):
        self.run_with(self.statuses("AVAILABLE"))
        self.sent.clear()
        self.run_with(self.statuses("LOW_AVAILABILITY"))
        self.assertEqual(self.sent, [])
        self.run_with(self.statuses("SOLD_OUT"))
        self.run_with(self.statuses("AVAILABLE"))
        self.assertEqual(len(self.sent), 1)

    def test_failed_ntfy_does_not_accept_availability_and_is_retried(self):
        self.run_with(self.statuses("SOLD_OUT"))
        before = self.state()
        self.sent.clear()
        self.assertEqual(self.run_with(self.statuses("AVAILABLE"), ntfy_ok=False)[0], 2)
        self.assertEqual(self.state()["last_observed"], before["last_observed"])
        self.sent.clear()
        self.assertEqual(self.run_with(self.statuses("AVAILABLE"))[0], 0)
        self.assertEqual(len(self.sent), 1)

    def test_api_error_does_not_overwrite_previous_availability(self):
        self.run_with(self.statuses("SOLD_OUT"))
        before = self.state()["last_observed"].copy()
        errors = self.statuses("SOLD_OUT")
        errors["2026-09-26"] = vw.VaticanError("błąd serwera", retry_after=300)
        self.assertEqual(self.run_with(errors)[0], 2)
        self.assertEqual(self.state()["last_observed"], before)
        self.assertEqual(self.state()["failure"]["count"], 1)

    def test_missing_does_not_overwrite_previous_state_or_spam_warning(self):
        available = self.statuses("AVAILABLE")
        self.run_with(available)
        self.sent.clear()
        missing = self.statuses("SOLD_OUT")
        missing["2026-09-27"] = "MISSING"
        self.run_with(missing)
        state = self.state()
        self.assertEqual(state["last_observed"]["2026-09-27"], "AVAILABLE")
        self.assertEqual(state["last_missing"], {"2026-09-27": "MISSING"})
        self.assertEqual(self.sent, [])
        with mock.patch.object(vw, "log") as logged:
            self.run_with(missing)
        self.assertNotIn("nie wystawia zwykłego", " ".join(map(str, logged.call_args_list)))

    def test_start_notification_and_finished_range(self):
        self.run_with(self.statuses("SOLD_OUT"), announce=True)
        self.assertEqual(len(self.sent), 1)
        self.sent.clear()
        with mock.patch.object(vw, "fetch_status", side_effect=AssertionError("bez API")):
            self.assertEqual(vw.run_once(today=date(2026, 9, 29)), (0, None))
        self.assertEqual(self.sent, [])

    def test_failure_alarm_once_then_recovery(self):
        error_statuses = self.statuses("SOLD_OUT")
        error_statuses["2026-09-24"] = vw.VaticanError("429", retry_after=120)
        for _ in range(3):
            self.run_with(error_statuses)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("problem z API", self.sent[0][0][1])
        self.sent.clear()
        self.run_with(self.statuses("SOLD_OUT"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("znów działa", self.sent[0][0][1])


if __name__ == "__main__":
    unittest.main()
