#!/usr/bin/env python3
"""Testy Watykan Watch, bez prawdziwego ntfy i bez zależności od sieci."""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vatican_watch"))
import check_vatican as vw  # noqa: E402


def raw_product(name="Musei Vaticani - Biglietti d'ingresso", status="SOLD_OUT", **changes):
    base = {
        "id": 123,
        "name": name,
        "suggestion": "",
        "availability": status,
        "description": "Ingresso ai Musei Vaticani",
        "descrAvailability": "Non disponibile",
        "priceFrom": "25,00 €",
        "numberParticipants": "1-10",
        "who": [{"id": 1, "description": "Adulti"}],
    }
    base.update(changes)
    return base


def offer(name="Bilet standardowy", status="SOLD_OUT", **changes):
    base = {
        "key": name.casefold(),
        "name": name,
        "availability": status,
        "description": "Opis",
        "message": "Brak miejsc",
        "price": "25,00 €",
        "suggestion": "",
        "participants": "1-10",
        "visitor_types": ["1:Adulti"],
    }
    base.update(changes)
    return base


class ConfigTest(unittest.TestCase):
    def test_defaults_monitor_one_visitor_as_broadest_signal(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = vw.load_config()
        self.assertEqual((cfg["start_date"], cfg["end_date"], cfg["visitors"], cfg["interval"]),
                         (date(2026, 9, 24), date(2026, 9, 28), 1, 60))

    def test_custom_visitors_and_invalid_values(self):
        with mock.patch.dict(os.environ, {"VISITORS": "5"}, clear=True):
            self.assertEqual(vw.load_config()["visitors"], 5)
        for value in ("0", "21", "x"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"VISITORS": value}, clear=True):
                with self.assertRaises(ValueError):
                    vw.load_config()

    def test_multiple_ticket_types_and_statuses_are_normalized(self):
        with mock.patch.dict(os.environ, {
                "TICKET_TYPES": "  Muséi Vaticani ; visita GUIDATA ; Muséi Vaticani ",
                "TICKET_STATUSES": " available, LOW_AVAILABILITY, available "}, clear=True):
            cfg = vw.load_config()
        self.assertEqual(cfg["ticket_types"], ["musei vaticani", "visita guidata"])
        self.assertEqual(cfg["ticket_statuses"], ["AVAILABLE", "LOW_AVAILABILITY"])

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


class SnapshotTest(unittest.TestCase):
    def test_every_product_type_is_preserved(self):
        payload = {"visits": [
            raw_product(),
            raw_product("Visita guidata", "AVAILABLE", id=2, suggestion="Suggerito"),
            raw_product("Castel Gandolfo", "LOW_AVAILABILITY", id=3),
        ]}
        snapshot = vw.snapshot_from_payload(payload)
        self.assertEqual([item["name"] for item in snapshot],
                         ["Castel Gandolfo", "Musei Vaticani - Biglietti d'ingresso", "Visita guidata"])

    def test_unstable_id_and_image_do_not_create_a_change(self):
        first = vw.snapshot_from_payload({"visits": [raw_product(id=1, image="a.jpg")]})
        second = vw.snapshot_from_payload({"visits": [raw_product(id=999, image="b.jpg")]})
        self.assertEqual(first, second)
        self.assertEqual(vw.diff_snapshots(first, second, date(2026, 9, 24)), [])

    def test_semantic_fields_are_canonicalized(self):
        item = vw.snapshot_from_payload({"visits": [raw_product(
            status=" available ", descrAvailability="  Są   miejsca ",
            who=[{"id": 2, "description": "Ridotto"}, {"id": 1, "description": "Adulti"}])]} )[0]
        self.assertEqual(item["availability"], "AVAILABLE")
        self.assertEqual(item["message"], "Są miejsca")
        self.assertEqual(item["visitor_types"], ["1:Adulti", "2:Ridotto"])

    def test_malformed_payload_is_contract_error(self):
        for payload in ({"oops": []}, {"visits": ["bad"]}, {"visits": [{"name": ""}]}):
            with self.subTest(payload=payload), self.assertRaises(vw.VaticanError):
                vw.snapshot_from_payload(payload)

    def test_add_remove_and_field_changes_are_detected(self):
        day = date(2026, 9, 24)
        old = [offer("A"), offer("B")]
        new = [offer("B", "AVAILABLE", message="Są miejsca", price="30,00 €"), offer("C")]
        changes = vw.diff_snapshots(old, new, day)
        self.assertEqual([change["kind"] for change in changes], ["added", "removed", "changed"])
        self.assertEqual(changes[-1]["fields"], ["availability", "message", "price"])
        self.assertTrue(vw.changes_include_availability(changes))

    def test_selected_changes_include_entering_and_losing_status(self):
        cfg = {"ticket_types": ["biglietti d'ingresso"], "ticket_statuses": ["AVAILABLE"]}
        selected = vw.canonical_product(raw_product(status="AVAILABLE"))
        unavailable = vw.canonical_product(raw_product(status="SOLD_OUT"))
        other = vw.canonical_product(raw_product("Visita guidata", "AVAILABLE"))
        self.assertTrue(vw.selected_product(selected, cfg))
        self.assertFalse(vw.selected_product(other, cfg))
        for previous, current in (([unavailable], [selected]), ([selected], [unavailable]),
                                  ([selected], []), ([], [selected])):
            changes = vw.diff_snapshots(previous, current, date(2026, 9, 24))
            self.assertTrue(any(vw.selected_change(change, cfg) for change in changes))
        self.assertFalse(vw.selected_change(
            vw.diff_snapshots([unavailable], [dict(unavailable, price="30,00 €")],
                              date(2026, 9, 24))[0], cfg))

    def test_pagination_downloads_all_results(self):
        pages = [
            {"totalResults": 3, "visits": [raw_product("A"), raw_product("B", id=2)]},
            {"totalResults": 3, "visits": [raw_product("C", id=3)]},
        ]
        with mock.patch.object(vw, "http_get_json", side_effect=pages) as get, \
                mock.patch.object(vw.time, "sleep"):
            result = vw.fetch_snapshot(date(2026, 9, 24), 1)
        self.assertEqual([item["name"] for item in result], ["A", "B", "C"])
        self.assertIn("page=0", get.call_args_list[0].args[0])
        self.assertIn("page=1", get.call_args_list[1].args[0])

    def test_incomplete_pagination_is_rejected(self):
        with mock.patch.object(vw, "http_get_json", side_effect=[
                {"totalResults": 2, "visits": [raw_product("A")]},
                {"totalResults": 2, "visits": []}]), mock.patch.object(vw.time, "sleep"):
            with self.assertRaisesRegex(vw.VaticanError, "totalResults"):
                vw.fetch_snapshot(date(2026, 9, 24), 1)


class RetryTest(unittest.TestCase):
    def test_result_url_is_official_angular_results_route_in_rome_time(self):
        self.assertEqual(vw.result_url(date(2026, 9, 24)),
                         "https://tickets.museivaticani.va/home/visit/1/1790200800000/1/")

    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(vw.parse_retry_after("42"), 42)
        then = datetime(2026, 9, 24, 12, 1, tzinfo=timezone.utc)
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(vw.parse_retry_after("Thu, 24 Sep 2026 12:01:00 GMT", now), 60)
        self.assertEqual(then.timestamp() - now.timestamp(), 60)

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
        state["snapshots"] = {"2026-09-24": [offer()]}
        state["visitors"] = 1
        vw.save_state(state)
        self.assertEqual(vw.load_state(), state)
        self.assertEqual([name for name in os.listdir(self.tmp.name) if name.endswith(".tmp")], [])

    def test_corrupt_or_old_state_starts_cleanly(self):
        with open(self.path, "w", encoding="utf-8") as state_file:
            state_file.write("not-json")
        self.assertEqual(vw.load_state(), vw.default_state())
        with open(self.path, "w", encoding="utf-8") as state_file:
            json.dump({"last_observed": {"2026-09-24": "SOLD_OUT"}}, state_file)
        self.assertEqual(vw.load_state(), vw.default_state())


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
            "VISITORS": "1", "NTFY_TOPIC": "unit-test-topic",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sent = []

    def snapshots(self, value=None):
        value = value or [offer()]
        return {f"2026-09-{day:02d}": value for day in range(24, 29)}

    def run_with(self, snapshots, ntfy_ok=True, today=date(2026, 9, 24), announce=False):
        def fetch(day, _visitors):
            value = snapshots[day.isoformat()]
            if isinstance(value, Exception):
                raise value
            return value

        def notify(*args, **kwargs):
            self.sent.append((args, kwargs))
            return ntfy_ok

        with mock.patch.object(vw, "fetch_snapshot", side_effect=fetch), \
                mock.patch.object(vw, "ntfy_post", side_effect=notify), \
                mock.patch.object(vw.time, "sleep"):
            return vw.run_once(announce_startup=announce, today=today)

    def state(self):
        return vw.load_state()

    def test_first_run_builds_baseline_without_change_alert(self):
        self.assertEqual(self.run_with(self.snapshots())[0], 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.state()["snapshots"]), 5)
        self.assertEqual(self.state()["visitors"], 1)

    def test_any_offer_change_alerts_and_availability_is_high_priority(self):
        self.run_with(self.snapshots())
        changed = self.snapshots()
        changed["2026-09-26"] = [offer(status="AVAILABLE", message="Są miejsca")]
        self.run_with(changed)
        self.assertEqual(len(self.sent), 1)
        args, kwargs = self.sent[0]
        self.assertIn("1 zmian", args[1])
        self.assertIn("status", args[2])
        self.assertEqual(kwargs["priority"], "high")
        self.assertIn("/visit/1/", kwargs["click"])

    def test_price_or_text_change_uses_default_priority(self):
        self.run_with(self.snapshots())
        changed = self.snapshots()
        changed["2026-09-24"] = [offer(price="26,00 €", message="Nowy komunikat")]
        self.run_with(changed)
        self.assertEqual(self.sent[0][1]["priority"], "default")

    def test_addition_and_removal_are_reported(self):
        self.run_with(self.snapshots())
        changed = self.snapshots()
        changed["2026-09-24"] = [offer("Nowa wycieczka", "AVAILABLE")]
        self.run_with(changed)
        body = self.sent[0][0][2]
        self.assertIn("➕", body)
        self.assertIn("➖", body)

    def test_failed_ntfy_keeps_baseline_and_retries_change(self):
        self.run_with(self.snapshots())
        before = self.state()
        changed = self.snapshots([offer(price="30,00 €")])
        self.assertEqual(self.run_with(changed, ntfy_ok=False)[0], 2)
        self.assertEqual(self.state(), before)
        self.sent.clear()
        self.assertEqual(self.run_with(changed)[0], 0)
        self.assertEqual(len(self.sent), 1)

    def test_api_error_does_not_overwrite_previous_snapshot(self):
        self.run_with(self.snapshots())
        before = self.state()["snapshots"].copy()
        errors = self.snapshots()
        errors["2026-09-26"] = vw.VaticanError("błąd serwera", retry_after=300)
        self.assertEqual(self.run_with(errors)[0], 2)
        self.assertEqual(self.state()["snapshots"], before)
        self.assertEqual(self.state()["failure"]["count"], 1)

    def test_visitors_change_creates_new_baseline_without_false_alert(self):
        self.run_with(self.snapshots())
        self.sent.clear()
        with mock.patch.dict(os.environ, {"VISITORS": "5"}, clear=False):
            self.run_with(self.snapshots([offer(price="99,00 €")]))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state()["visitors"], 5)

    def test_ticket_selection_filters_notifications_and_rebaselines(self):
        initial = self.snapshots([offer("Bilet standardowy", "SOLD_OUT"),
                                  offer("Wycieczka", "AVAILABLE")])
        self.run_with(initial)
        self.sent.clear()
        with mock.patch.dict(os.environ, {"TICKET_TYPES": "Bilet standardowy",
                                      "TICKET_STATUSES": "AVAILABLE"}):
            self.run_with(initial)
            self.assertEqual(self.sent, [])
            self.assertEqual(len(self.state()["snapshots"]["2026-09-24"]), 1)
            available = self.snapshots([offer("Bilet standardowy", "AVAILABLE"),
                                        offer("Wycieczka", "SOLD_OUT")])
            self.run_with(available)
            self.assertEqual(len(self.sent), 1)
            self.assertIn("SOLD_OUT → AVAILABLE", self.sent[-1][0][2])
            self.sent.clear()
            self.run_with(initial)
            self.assertEqual(len(self.sent), 1)
            self.assertIn("AVAILABLE → SOLD_OUT", self.sent[-1][0][2])
            self.sent.clear()
            self.run_with(self.snapshots([offer("Wycieczka", "AVAILABLE")]))
            self.assertEqual(self.sent, [])

    def test_start_notification_and_finished_range(self):
        self.run_with(self.snapshots(), announce=True)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("wszystkie oferty", self.sent[0][0][2])
        self.sent.clear()
        with mock.patch.object(vw, "fetch_snapshot", side_effect=AssertionError("bez API")):
            self.assertEqual(vw.run_once(today=date(2026, 9, 29)), (0, None))
        self.assertEqual(self.sent, [])

    def test_failure_alarm_once_then_recovery(self):
        errors = self.snapshots()
        errors["2026-09-24"] = vw.VaticanError("429", retry_after=120)
        for _ in range(3):
            self.run_with(errors)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("problem z API", self.sent[0][0][1])
        self.sent.clear()
        self.run_with(self.snapshots())
        self.assertEqual(len(self.sent), 1)
        self.assertIn("znów działa", self.sent[0][0][1])


if __name__ == "__main__":
    unittest.main()
