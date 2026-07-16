#!/usr/bin/env python3
"""Testy jednostkowe silnika (bez sieci). Uruchomienie: python3 -m unittest -v test_check_padel"""

import io
import base64
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "padel_watch"))
import check_padel as cp  # noqa: E402

TZ = ZoneInfo("Europe/Warsaw")
FILTERS = [
    {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "15:00", "end": "02:00"},
    {"days": ["sat", "sun"], "start": "00:00", "end": "24:00"},
]


def jwt_with_exp(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode("utf-8")).decode("ascii").rstrip("=")
    return f"header.{payload}.signature"


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

    def test_save_preserves_registered_ids(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cp, "STATE_PATH", os.path.join(td, "state.json")):
                cp.save_state({"a"}, {"old"})
                cp.save_state({"b"})
                self.assertEqual(cp.load_registered_ids(), {"old"})

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


class TestAutoRegister(unittest.TestCase):
    SLOT = {
        "id": "L:D",
        "listing_id": "L",
        "date_id": "D",
        "start_utc": datetime(2026, 7, 7, 10, 0, tzinfo=timezone.utc),
        "price": None,
    }

    def test_missing_token_skips(self):
        ok, msg = cp.register_slot(self.SLOT, None, {"enabled": True, "name": "Jan Kowalski"})
        self.assertFalse(ok)
        self.assertIn("tokenu", msg)

    def test_paid_slot_skips_by_default(self):
        slot = dict(self.SLOT, price={"currency": "PLN", "amount": 1500})
        ok, msg = cp.register_slot(slot, None, {"token": "t", "name": "Jan Kowalski", "free_only": True})
        self.assertFalse(ok)
        self.assertIn("płatny", msg)

    def test_register_payload(self):
        seen = {}

        def fake_rpc(method, token, payload):
            seen["method"] = method
            seen["token"] = token
            seen["payload"] = payload
            return {"processState": "accepted"}

        cfg = {"token": "jwt", "name": "Jan Kowalski", "age": "34", "free_only": True}
        with mock.patch.object(cp, "decathlon_rpc", fake_rpc):
            ok, msg = cp.register_slot(self.SLOT, None, cfg)
        self.assertTrue(ok)
        self.assertEqual(msg, "accepted")
        self.assertEqual(seen["method"], "transactions.create")
        self.assertEqual(seen["payload"]["listingDateId"], "D")
        self.assertEqual(seen["payload"]["participants"][0]["name"], "Jan Kowalski")

    def test_token_is_cleaned(self):
        self.assertEqual(cp.clean_decathlon_token('JWT: "abc.def.ghi"'), "abc.def.ghi")
        self.assertEqual(cp.clean_decathlon_token("Bearer abc.def.ghi"), "abc.def.ghi")

    def test_newer_state_token_wins(self):
        old = jwt_with_exp(100)
        new = jwt_with_exp(200)
        self.assertEqual(cp.newer_decathlon_token(old, new), new)
        self.assertEqual(cp.newer_decathlon_token(new, old), new)

    def test_register_refreshes_token_after_401(self):
        seen = []

        def fake_rpc(method, token, payload):
            seen.append(token)
            if len(seen) == 1:
                raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
            return {"processState": "accepted"}

        def fake_refresh(token, cookie):
            self.assertEqual(token, "old.jwt.token")
            self.assertEqual(cookie, "sid=1")
            return "new.jwt.token"

        cfg = {
            "token": "old.jwt.token",
            "refresh_cookie": "sid=1",
            "name": "Jan Kowalski",
            "free_only": True,
        }
        with mock.patch.object(cp, "decathlon_rpc", fake_rpc), \
                mock.patch.object(cp, "refresh_decathlon_token", fake_refresh):
            ok, msg = cp.register_slot(self.SLOT, None, cfg, speculative=True)

        self.assertTrue(ok)
        self.assertIn("walidacja OK", msg)
        self.assertEqual(seen, ["old.jwt.token", "new.jwt.token"])
        self.assertEqual(cfg["token"], "new.jwt.token")
        self.assertTrue(cfg["token_refreshed"])

    def test_decathlon_rpc_wraps_input(self):
        seen = {}

        class FakeResponse(io.BytesIO):
            headers = {"get": staticmethod(lambda name: None)}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(req, timeout=30):
            seen["url"] = req.full_url
            seen["headers"] = dict(req.header_items())
            seen["payload"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse(json.dumps({"output": {"processState": "accepted"}}).encode("utf-8"))

        with mock.patch.object(cp.urllib.request, "urlopen", fake_urlopen):
            doc = cp.decathlon_rpc("transactions.create", "jwt", {"listingDateId": "D"})

        self.assertEqual(doc, {"processState": "accepted"})
        self.assertEqual(seen["url"], "https://go.decathlon.pl/api/v2/transactions.create")
        self.assertEqual(seen["payload"]["input"], {"listingDateId": "D"})
        self.assertEqual(seen["payload"]["extend"], {})


class TestAutoRegisterLimits(unittest.TestCase):
    """Bezpieczniki: limit na przebieg, przerwanie po błędzie auth, kolejność."""

    @staticmethod
    def slots(n):
        return [
            {"id": f"L:{i}", "listing_id": "L", "date_id": f"D{i}",
             "start_utc": datetime(2026, 7, 7, 9 + i, 0, tzinfo=timezone.utc), "price": None}
            for i in range(n)
        ]

    def run_auto(self, slots, cfg, side_effect):
        calls = []

        def fake_register(slot, price, c, speculative=False):
            calls.append(slot["id"])
            return side_effect(slot)

        with mock.patch.object(cp, "register_slot", fake_register):
            results, registered = cp.auto_register_new_slots(slots, {}, cfg, set())
        return calls, results, registered

    def test_default_limit_is_one(self):
        cfg = {"enabled": True}  # brak max_per_run -> domyślnie 1
        calls, _, registered = self.run_auto(self.slots(5), cfg, lambda s: (True, "ok"))
        self.assertEqual(len(calls), 1, "bez limitu zarezerwowałoby wszystkie!")
        self.assertEqual(registered, {"L:0"})

    def test_limit_respected(self):
        cfg = {"enabled": True, "max_per_run": 2}
        calls, _, registered = self.run_auto(self.slots(5), cfg, lambda s: (True, "ok"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(registered, {"L:0", "L:1"})

    def test_earliest_slot_first(self):
        cfg = {"enabled": True, "max_per_run": 1}
        shuffled = list(reversed(self.slots(4)))  # najpóźniejszy na początku listy
        calls, _, _ = self.run_auto(shuffled, cfg, lambda s: (True, "ok"))
        self.assertEqual(calls, ["L:0"], "powinien wybrać najwcześniejszy termin")

    def test_zero_limit_registers_nothing(self):
        cfg = {"enabled": True, "max_per_run": 0}
        calls, _, registered = self.run_auto(self.slots(3), cfg, lambda s: (True, "ok"))
        self.assertEqual(calls, [])
        self.assertEqual(registered, set())

    def test_auth_failure_aborts_run(self):
        cfg = {"enabled": True, "max_per_run": 5}
        calls, _, _ = self.run_auto(
            self.slots(5), cfg, lambda s: (False, "token odrzucony (HTTP 401) — sprawdź cookie"))
        self.assertEqual(len(calls), 1, "po błędzie auth nie wolno dobijać się kolejnymi slotami")

    def test_non_auth_failure_continues(self):
        cfg = {"enabled": True, "max_per_run": 5}
        calls, _, _ = self.run_auto(self.slots(3), cfg, lambda s: (False, "termin płatny — pomijam"))
        self.assertEqual(len(calls), 3, "zwykłe pominięcie nie przerywa przebiegu")

    def test_speculative_does_not_mark_registered(self):
        cfg = {"enabled": True, "max_per_run": 2, "speculative": True}
        _, _, registered = self.run_auto(self.slots(3), cfg, lambda s: (True, "walidacja OK"))
        self.assertEqual(registered, set())

    def test_already_registered_not_retried(self):
        cfg = {"enabled": True, "max_per_run": 5}
        slots = self.slots(2)
        with mock.patch.object(cp, "register_slot", lambda *a, **k: (True, "ok")):
            results, registered = cp.auto_register_new_slots(slots, {}, cfg, {"L:0"})
        self.assertEqual(results["L:0"], (True, "już zarejestrowane"))
        self.assertIn("L:1", registered)

    def test_disabled_does_nothing(self):
        calls, results, registered = self.run_auto(self.slots(3), {"enabled": False}, lambda s: (True, "ok"))
        self.assertEqual((calls, results, registered), ([], {}, set()))

    def test_order_latest_first(self):
        cfg = {"enabled": True, "max_per_run": 1, "order": "latest"}
        calls, _, _ = self.run_auto(self.slots(4), cfg, lambda s: (True, "ok"))
        self.assertEqual(calls, ["L:3"], "przy order=latest bierze najpóźniejszy termin")

    def test_order_earliest_is_default(self):
        calls, _, _ = self.run_auto(self.slots(4), {"enabled": True}, lambda s: (True, "ok"))
        self.assertEqual(calls, ["L:0"])

    def test_order_latest_respects_limit_and_sequence(self):
        cfg = {"enabled": True, "max_per_run": 2, "order": "latest"}
        calls, _, _ = self.run_auto(self.slots(5), cfg, lambda s: (True, "ok"))
        self.assertEqual(calls, ["L:4", "L:3"], "od najpóźniejszego, malejąco")

    def test_unknown_order_falls_back_to_earliest(self):
        cfg = {"enabled": True, "max_per_run": 1, "order": "bzdura"}
        calls, _, _ = self.run_auto(self.slots(3), cfg, lambda s: (True, "ok"))
        self.assertEqual(calls, ["L:0"])


class TestPendingAfterAuthFailure(unittest.TestCase):
    """Po awarii tokenu zapamiętujemy termin(y) do ponowienia — ale nie hurtowo."""

    @staticmethod
    def slots(n):
        base = datetime(2026, 7, 7, 9, 0, tzinfo=timezone.utc)
        return [
            {"id": f"L:{i}", "listing_id": "L", "date_id": f"D{i}",
             "start_utc": base + timedelta(hours=i), "price": None}
            for i in range(n)
        ]

    def test_auth_failure_records_pending_limited_to_max(self):
        cfg = {"enabled": True, "max_per_run": 1}
        with mock.patch.object(cp, "register_slot", lambda *a, **k: (False, "token odrzucony (HTTP 401)")):
            cp.auto_register_new_slots(self.slots(30), {}, cfg, set())
        self.assertEqual(cfg["auth_error"], "token odrzucony (HTTP 401)")
        self.assertEqual(cfg["pending_ids"], ["L:0"],
                         "zapamiętujemy tylko tyle, ile zapisalibyśmy (max_per_run)")

    def test_pending_respects_latest_order(self):
        cfg = {"enabled": True, "max_per_run": 2, "order": "latest"}
        with mock.patch.object(cp, "register_slot", lambda *a, **k: (False, "brak tokenu Decathlon GO")):
            cp.auto_register_new_slots(self.slots(5), {}, cfg, set())
        self.assertEqual(cfg["pending_ids"], ["L:4", "L:3"])

    def test_success_clears_pending_and_auth_error(self):
        cfg = {"enabled": True, "max_per_run": 1}
        with mock.patch.object(cp, "register_slot", lambda *a, **k: (True, "accepted")):
            cp.auto_register_new_slots(self.slots(3), {}, cfg, set())
        self.assertIsNone(cfg["auth_error"])
        self.assertEqual(cfg["pending_ids"], [])

    def test_non_auth_failure_is_not_pending(self):
        cfg = {"enabled": True, "max_per_run": 1}
        with mock.patch.object(cp, "register_slot", lambda *a, **k: (False, "termin płatny — pomijam")):
            cp.auto_register_new_slots(self.slots(2), {}, cfg, set())
        self.assertIsNone(cfg["auth_error"])
        self.assertEqual(cfg["pending_ids"], [], "płatny termin nie jest 'do ponowienia'")


class TestStatePendingAndAlert(unittest.TestCase):
    """Trwałość pending_ids / auth_alert_sent w state.json."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "state.json")
        p = mock.patch.object(cp, "STATE_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_pending_and_alert_persisted(self):
        cp.save_state({"L:1"}, set(), pending_ids=["L:1"], auth_alert_sent=True)
        d = self.read()
        self.assertEqual(d["pending_ids"], ["L:1"])
        self.assertTrue(d["auth_alert_sent"])

    def test_pending_carried_over_when_not_passed(self):
        cp.save_state({"L:1"}, set(), pending_ids=["L:1"], auth_alert_sent=True)
        cp.save_state({"L:1", "L:2"})  # bez podania -> ma przenieść poprzednie
        d = self.read()
        self.assertEqual(d["pending_ids"], ["L:1"])
        self.assertTrue(d["auth_alert_sent"])

    def test_alert_cleared_explicitly(self):
        cp.save_state({"L:1"}, set(), pending_ids=["L:1"], auth_alert_sent=True)
        cp.save_state({"L:1"}, set(), pending_ids=[], auth_alert_sent=False)
        d = self.read()
        self.assertNotIn("pending_ids", d)
        self.assertNotIn("auth_alert_sent", d)


class TestClearState(unittest.TestCase):
    """Jednorazowe czyszczenie stanu (opcja clear_state)."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "state.json")
        patcher = mock.patch.object(cp, "STATE_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("CLEAR_STATE", "CONFIG_PATH"):
            os.environ.pop(var, None)
        os.environ["CONFIG_PATH"] = os.path.join(self.td.name, "brak.json")
        self.addCleanup(lambda: os.environ.pop("CLEAR_STATE", None))
        cp.CONFIG_PATH = os.path.join(self.td.name, "brak.json")

    def seed(self, **extra):
        doc = {"free_ids": ["L:1"], "registered_ids": ["L:1", "L:2"], "decathlon_jwt": "a.b.c"}
        doc.update(extra)
        cp.write_state_doc(doc)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_clear_registered_only(self):
        self.seed()
        os.environ["CLEAR_STATE"] = "registered"
        cp.apply_clear_state()
        d = self.read()
        self.assertEqual(d["registered_ids"], [])
        self.assertEqual(d["free_ids"], ["L:1"], "śledzone terminy zostają")
        self.assertEqual(d["decathlon_jwt"], "a.b.c", "token zostaje")
        self.assertEqual(d["clear_state_applied"], "registered")

    def test_clear_all_wipes_everything(self):
        self.seed()
        os.environ["CLEAR_STATE"] = "all"
        cp.apply_clear_state()
        d = self.read()
        self.assertEqual(d["registered_ids"], [])
        self.assertEqual(d["free_ids"], [])
        self.assertNotIn("decathlon_jwt", d, "'all' kasuje też token")

    def test_is_one_shot_across_restarts(self):
        self.seed()
        os.environ["CLEAR_STATE"] = "registered"
        cp.apply_clear_state()
        cp.save_state({"L:9"}, {"L:9"})          # nowy zapis po czyszczeniu
        cp.apply_clear_state()                    # "restart" z tą samą opcją
        self.assertEqual(self.read()["registered_ids"], ["L:9"], "nie wolno czyścić ponownie")

    def test_marker_survives_save_state(self):
        self.seed()
        os.environ["CLEAR_STATE"] = "registered"
        cp.apply_clear_state()
        cp.save_state({"L:5"}, {"L:5"})
        self.assertEqual(self.read()["clear_state_applied"], "registered")

    def test_changed_value_clears_again(self):
        self.seed()
        os.environ["CLEAR_STATE"] = "registered"
        cp.apply_clear_state()
        cp.save_state({"L:9"}, {"L:9"})
        os.environ["CLEAR_STATE"] = "all"        # zmiana wartości -> czyść znowu
        cp.apply_clear_state()
        self.assertEqual(self.read()["registered_ids"], [])

    def test_empty_option_does_nothing(self):
        self.seed()
        os.environ["CLEAR_STATE"] = ""
        cp.apply_clear_state()
        self.assertEqual(self.read()["registered_ids"], ["L:1", "L:2"])

    def test_no_state_file_is_safe(self):
        os.environ["CLEAR_STATE"] = "all"
        cp.apply_clear_state()  # nie może rzucić
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
