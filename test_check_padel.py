#!/usr/bin/env python3
"""Testy jednostkowe silnika (bez sieci). Uruchomienie: python3 -m unittest -v test_check_padel"""

import io
import gzip
import base64
import json
import contextlib
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "padel_browser"))
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
        self.assertEqual(w[0]["seconds"], cp.MIN_INTERVAL_SECONDS)

    def test_minimum_is_two_seconds(self):
        self.assertEqual(cp.MIN_INTERVAL_SECONDS, 2)

    def test_value_above_minimum_is_kept(self):
        w = cp.parse_intervals_env("mon-fri:10:45-11:15=3")
        self.assertEqual(w[0]["seconds"], 3, "wartość >= minimum nie może być zmieniana")

    def test_clamp_is_logged(self):
        with mock.patch.object(cp, "log") as fake_log:
            cp.parse_intervals_env("mon-fri:10:00-12:00=1")
        self.assertTrue(any("żądano 1s" in str(c) for c in fake_log.call_args_list),
                        "podbicie do minimum musi być widoczne w logu")

    def test_aggressive_value_warns(self):
        with mock.patch.object(cp, "log") as fake_log:
            cp.parse_intervals_env("mon-fri:10:45-11:15=3")
        self.assertTrue(any("agresywne" in str(c) for c in fake_log.call_args_list))

    def test_normal_value_does_not_warn(self):
        with mock.patch.object(cp, "log") as fake_log:
            cp.parse_intervals_env("mon-fri:17:00-22:00=30")
        self.assertEqual(fake_log.call_args_list, [])

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

        def fake_refresh(token, cookie=None, refresh_token=None):
            self.assertEqual(token, "old.jwt.token")
            return ("new.jwt.token", "")

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

        # rejestracja idzie po podtrzymanym połączeniu (open_url), nie przez urlopen
        with mock.patch.object(cp, "open_url", fake_urlopen):
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


class TestCookieOnlyToken(unittest.TestCase):
    """decathlon_token jest opcjonalny — wystarczy decathlon_cookie."""

    @staticmethod
    def fresh_jwt():
        return jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)

    @staticmethod
    def expired_jwt():
        return jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) - 10)

    def test_no_token_but_cookie_fetches_one(self):
        seen = {}

        def fake_refresh(token, cookie=None, refresh_token=None):
            seen["token"] = token
            seen["cookie"] = cookie
            return ("swiezy.jwt.token", "")

        cfg = {"token": "", "refresh_cookie": "sid=abc"}
        with mock.patch.object(cp, "refresh_decathlon_token", fake_refresh):
            token, err = cp.ensure_decathlon_token(cfg)
        self.assertIsNone(err)
        self.assertEqual(token, "swiezy.jwt.token")
        self.assertEqual(seen["cookie"], "sid=abc")
        self.assertEqual(seen["token"], "", "bez tokenu refresh opiera się na samym cookie")
        self.assertEqual(cfg["token"], "swiezy.jwt.token")

    def test_expired_token_refreshed_proactively(self):
        cfg = {"token": self.expired_jwt(), "refresh_cookie": "sid=abc"}
        with mock.patch.object(cp, "refresh_decathlon_token", lambda t, c=None, r=None: ("nowy.jwt.token", "")):
            token, err = cp.ensure_decathlon_token(cfg)
        self.assertIsNone(err)
        self.assertEqual(token, "nowy.jwt.token")

    def test_valid_token_is_not_refreshed(self):
        valid = self.fresh_jwt()
        cfg = {"token": valid, "refresh_cookie": "sid=abc"}
        with mock.patch.object(cp, "refresh_decathlon_token",
                               side_effect=AssertionError("nie wolno odświeżać ważnego tokenu")):
            token, err = cp.ensure_decathlon_token(cfg)
        self.assertIsNone(err)
        self.assertEqual(token, valid)

    def test_no_token_no_cookie_is_auth_error(self):
        token, err = cp.ensure_decathlon_token({"token": "", "refresh_cookie": ""})
        self.assertIsNone(token)
        self.assertIn("brak tokenu", err)
        self.assertTrue(any(m in err for m in cp.AUTH_FAILURE_MARKERS),
                        "musi być rozpoznane jako blad auth (przerywa przebieg)")

    def test_refresh_failure_is_auth_error(self):
        cfg = {"token": "", "refresh_cookie": "sid=abc"}
        with mock.patch.object(cp, "refresh_decathlon_token",
                               side_effect=urllib.error.URLError("brak sieci")):
            token, err = cp.ensure_decathlon_token(cfg)
        self.assertIn("nie udało się odświeżyć tokenu", err)
        self.assertTrue(any(m in err for m in cp.AUTH_FAILURE_MARKERS))

    def test_unparsable_token_without_cookie_is_used_as_is(self):
        cfg = {"token": "nie-jest-jwt", "refresh_cookie": ""}
        token, err = cp.ensure_decathlon_token(cfg)
        self.assertEqual(token, "nie-jest-jwt")
        self.assertIsNone(err, "nie znamy exp -> próbujemy, 401 obsłuży fallback")

    def test_register_works_with_cookie_only(self):
        slot = {"id": "L:D", "date_id": "D", "price": None,
                "start_utc": datetime(2026, 7, 7, 10, 0, tzinfo=timezone.utc)}
        cfg = {"token": "", "refresh_cookie": "sid=abc", "name": "Jan Kowalski", "free_only": True}
        with mock.patch.object(cp, "refresh_decathlon_token", lambda t, c=None, r=None: ("swiezy.jwt.token", "")), \
                mock.patch.object(cp, "decathlon_rpc", lambda m, t, p: {"processState": "accepted"}):
            ok, msg = cp.register_slot(slot, None, cfg)
        self.assertTrue(ok, f"zapis samym cookie powinien przejść, dostałem: {msg}")

    def test_refresh_omits_auth_header_when_no_token(self):
        seen = {}

        class FakeResponse(io.BytesIO):
            headers = {"get": staticmethod(lambda n: None)}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=30):
            seen["headers"] = dict(req.header_items())
            return FakeResponse(json.dumps({"jwt": "swiezy.jwt.token"}).encode("utf-8"))

        with mock.patch.object(cp.urllib.request, "urlopen", fake_urlopen):
            out, _rt = cp.refresh_decathlon_token("", "sid=abc")
        self.assertEqual(out, "swiezy.jwt.token")
        keys = {k.lower() for k in seen["headers"]}
        self.assertIn("cookie", keys)
        self.assertNotIn("authorization", keys, "bez tokenu nie wysyłamy pustego Bearer")


class TestJwtOnlyAuth(unittest.TestCase):
    """Decathlon GO trzyma auth w localStorage: wystarczy sam go-sdk-jwt (bez cookie)."""

    @staticmethod
    def expired():
        return jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) - 10)

    def test_refresh_sends_only_bearer_when_no_cookie_no_rt(self):
        seen = {}

        class FakeResponse(io.BytesIO):
            headers = {"get": staticmethod(lambda n: None)}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=30):
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            seen["body"] = json.loads(req.data.decode("utf-8"))
            seen["url"] = req.full_url
            return FakeResponse(json.dumps({"jwt": "nowy.jwt.token"}).encode("utf-8"))

        with mock.patch.object(cp.urllib.request, "urlopen", fake_urlopen):
            jwt, rt = cp.refresh_decathlon_token("stary.jwt.token")
        self.assertEqual((jwt, rt), ("nowy.jwt.token", ""))
        self.assertEqual(seen["url"], "https://go.decathlon.pl/api/auth/refresh")
        self.assertEqual(seen["headers"]["authorization"], "Bearer stary.jwt.token")
        self.assertNotIn("cookie", seen["headers"], "GO nie ma ciasteczka sesji")
        self.assertEqual(seen["body"], {}, "bez go-unsafe-rt body jest puste")

    def test_refresh_includes_rt_when_available(self):
        seen = {}

        class FakeResponse(io.BytesIO):
            headers = {"get": staticmethod(lambda n: None)}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=30):
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse(json.dumps({"jwt": "j2", "rt": "rt2"}).encode("utf-8"))

        with mock.patch.object(cp.urllib.request, "urlopen", fake_urlopen):
            jwt, rt = cp.refresh_decathlon_token("j1", None, "rt1")
        self.assertEqual(seen["body"], {"unsafeRefreshToken": "rt1"})
        self.assertEqual((jwt, rt), ("j2", "rt2"), "rotowany refresh token musi wrócić")

    def test_expiring_jwt_refreshed_without_cookie(self):
        cfg = {"token": self.expired(), "refresh_cookie": ""}
        with mock.patch.object(cp, "refresh_decathlon_token",
                               lambda t, c=None, r=None: ("swiezy.jwt.token", "")):
            token, err = cp.ensure_decathlon_token(cfg)
        self.assertIsNone(err, "sam JWT musi wystarczyć do odświeżenia")
        self.assertEqual(token, "swiezy.jwt.token")

    def test_rotated_rt_stored_in_cfg(self):
        cfg = {"token": self.expired(), "refresh_token": "rt1"}
        with mock.patch.object(cp, "refresh_decathlon_token",
                               lambda t, c=None, r=None: ("j2", "rt2")):
            cp.ensure_decathlon_token(cfg)
        self.assertEqual(cfg["refresh_token"], "rt2", "nowy rt musi nadpisać stary")

    def test_no_credentials_at_all(self):
        token, err = cp.ensure_decathlon_token({"token": "", "refresh_cookie": ""})
        self.assertIsNone(token)
        self.assertIn("go-sdk-jwt", err, "komunikat ma powiedzieć CO wkleić")

    def test_rt_persisted_in_state(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cp, "STATE_PATH", os.path.join(td, "state.json")):
                cp.save_state({"L:1"}, set(), decathlon_jwt="j1", decathlon_rt="rt1")
                cp.save_state({"L:1"})  # kolejny zapis bez podania -> ma przenieść
                with open(os.path.join(td, "state.json"), encoding="utf-8") as f:
                    d = json.load(f)
        self.assertEqual(d["decathlon_rt"], "rt1")


class TestTokenExpiryMargin(unittest.TestCase):
    """Margines musi dawać zapas na check_interval — inaczej token wygasa między biegami."""

    def test_margin_is_generous(self):
        self.assertGreaterEqual(
            cp.TOKEN_EXPIRY_MARGIN, 300,
            "margines 60s < typowy check_interval -> token wygasa miedzy sprawdzeniami, "
            "a refresh wygaslego JWT to 401")

    def test_token_refreshed_before_expiry_not_after(self):
        """Token ważny jeszcze 2 min musi być odświeżony ZAWCZASU (a nie dopiero po wygaśnięciu)."""
        soon = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 120)
        cfg = {"token": soon}
        with mock.patch.object(cp, "refresh_decathlon_token",
                               lambda t, c=None, r=None: ("swiezy.jwt.token", "")):
            token, err = cp.ensure_decathlon_token(cfg)
        self.assertIsNone(err)
        self.assertEqual(token, "swiezy.jwt.token",
                         "przy 2 min do konca i marginesie 300s trzeba odswiezyc")

    def test_token_with_long_life_not_refreshed(self):
        long_lived = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)
        cfg = {"token": long_lived}
        with mock.patch.object(cp, "refresh_decathlon_token",
                               side_effect=AssertionError("nie wolno odswiezac zywego tokenu")):
            token, err = cp.ensure_decathlon_token(cfg)
        self.assertEqual(token, long_lived)
        self.assertIsNone(err)


class TestVerifyToken(unittest.TestCase):
    """`token OK` musi znaczyc 'serwer potwierdzil', a nie 'exp wyglada dobrze'."""

    class _Resp(io.BytesIO):
        status = 200
        headers = {"get": staticmethod(lambda n: None)}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_valid_token_verified_against_server(self):
        seen = {}

        def fake_urlopen(req, timeout=30):
            seen["url"] = req.full_url
            seen["auth"] = dict(req.header_items()).get("Authorization")
            return self._Resp(b"{}")

        with mock.patch.object(cp.urllib.request, "urlopen", fake_urlopen):
            ok, detail = cp.verify_decathlon_token("a.b.c")
        self.assertTrue(ok)
        self.assertEqual(seen["url"], "https://go.decathlon.pl/api/user-consent/my-consents")
        self.assertEqual(seen["auth"], "Bearer a.b.c")

    def test_rejected_token_is_false(self):
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        with mock.patch.object(cp.urllib.request, "urlopen", side_effect=err):
            ok, detail = cp.verify_decathlon_token("a.b.c")
        self.assertFalse(ok)
        self.assertIn("401", detail)

    def test_network_error_is_unknown_not_failure(self):
        with mock.patch.object(cp.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("brak sieci")):
            ok, _ = cp.verify_decathlon_token("a.b.c")
        self.assertIsNone(ok, "awaria sieci to nie jest odrzucenie tokenu")

    def test_empty_token_short_circuits(self):
        with mock.patch.object(cp.urllib.request, "urlopen",
                               side_effect=AssertionError("nie wolno strzelac bez tokenu")):
            ok, _ = cp.verify_decathlon_token("")
        self.assertFalse(ok)

    def test_check_reports_failure_when_server_rejects(self):
        """Kluczowe: lokalnie wazny JWT, ale serwer go odrzuca -> musi byc ✗, nie ✓."""
        valid_locally = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)
        cfg = {"token": valid_locally}
        with mock.patch.object(cp, "verify_decathlon_token", lambda t: (False, "HTTP 401")), \
                mock.patch.object(cp, "notify_auth_problem") as fake_notify, \
                mock.patch.object(cp, "log") as fake_log:
            ok = cp.check_decathlon_credentials(cfg, topic="temat")
        self.assertFalse(ok)
        self.assertIn("ODRZUCIŁ", cfg["auth_error"])
        fake_notify.assert_called_once()
        self.assertTrue(any("✗" in str(c) for c in fake_log.call_args_list))

    def test_check_reports_success_when_server_accepts(self):
        valid = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)
        cfg = {"token": valid}
        with mock.patch.object(cp, "verify_decathlon_token", lambda t: (True, "HTTP 200")), \
                mock.patch.object(cp, "log") as fake_log:
            ok = cp.check_decathlon_credentials(cfg)
        self.assertTrue(ok)
        self.assertIsNone(cfg["auth_error"])
        joined = " ".join(str(c) for c in fake_log.call_args_list)
        self.assertIn("serwer potwierdził", joined)

    def test_network_unknown_does_not_raise_alert(self):
        valid = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)
        cfg = {"token": valid}
        with mock.patch.object(cp, "verify_decathlon_token", lambda t: (None, "sieć")), \
                mock.patch.object(cp, "notify_auth_problem") as fake_notify, \
                mock.patch.object(cp, "log"):
            ok = cp.check_decathlon_credentials(cfg, topic="temat")
        self.assertTrue(ok)
        self.assertIsNone(cfg["auth_error"])
        fake_notify.assert_not_called()


class TestCredentialSelfTest(unittest.TestCase):
    """Test poświadczeń działa BEZ wolnych terminów (opcja test_token / start)."""

    def test_ok_reports_expiry(self):
        exp = int(datetime.now(timezone.utc).timestamp()) + 3600
        cfg = {"token": "", "refresh_cookie": "sid=abc"}
        with mock.patch.object(cp, "refresh_decathlon_token", lambda t, c=None, r=None: (jwt_with_exp(exp), "")), \
                mock.patch.object(cp, "verify_decathlon_token", lambda t: (True, "HTTP 200")), \
                mock.patch.object(cp, "log") as fake_log:
            ok = cp.check_decathlon_credentials(cfg)
        self.assertTrue(ok)
        self.assertIsNone(cfg["auth_error"])
        joined = " ".join(str(c) for c in fake_log.call_args_list)
        self.assertIn("token DZIAŁA", joined)
        self.assertIn("Ważny do", joined, "log ma podać do kiedy token jest ważny")

    def test_missing_everything_reports_and_sets_error(self):
        cfg = {"token": "", "refresh_cookie": ""}
        with mock.patch.object(cp, "log"):
            ok = cp.check_decathlon_credentials(cfg)
        self.assertFalse(ok)
        self.assertIn("brak tokenu", cfg["auth_error"])

    def test_bad_cookie_notifies(self):
        cfg = {"token": "", "refresh_cookie": "sid=zle"}
        with mock.patch.object(cp, "refresh_decathlon_token",
                               side_effect=urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}"))), \
                mock.patch.object(cp, "notify_auth_problem") as fake_notify, \
                mock.patch.object(cp, "log"):
            ok = cp.check_decathlon_credentials(cfg, topic="temat")
        self.assertFalse(ok)
        self.assertIn("nie udało się odświeżyć tokenu", cfg["auth_error"])
        fake_notify.assert_called_once()

    def test_does_not_book_anything(self):
        """Test poświadczeń nie może niczego rezerwować."""
        cfg = {"token": "", "refresh_cookie": "sid=abc"}
        with mock.patch.object(cp, "refresh_decathlon_token", lambda t, c=None, r=None: ("a.b.c", "")), \
                mock.patch.object(cp, "verify_decathlon_token", lambda t: (True, "HTTP 200")), \
                mock.patch.object(cp, "register_slot", side_effect=AssertionError("nie wolno rezerwować!")), \
                mock.patch.object(cp, "decathlon_rpc", side_effect=AssertionError("nie wolno wołać RPC!")), \
                mock.patch.object(cp, "log"):
            self.assertTrue(cp.check_decathlon_credentials(cfg))

    def test_verification_is_a_plain_get(self):
        """Weryfikacja musi byc GET-em bez skutkow ubocznych (nie POST)."""
        seen = {}

        class _R(io.BytesIO):
            status = 200
            headers = {"get": staticmethod(lambda n: None)}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=30):
            seen["method"] = req.get_method()
            seen["data"] = req.data
            return _R(b"{}")

        with mock.patch.object(cp.urllib.request, "urlopen", fake_urlopen):
            cp.verify_decathlon_token("a.b.c")
        self.assertEqual(seen["method"], "GET")
        self.assertIsNone(seen["data"], "GET nie moze miec body")

    def test_marks_auth_checked(self):
        cfg = {"token": "", "refresh_cookie": ""}
        with mock.patch.object(cp, "log"):
            cp.check_decathlon_credentials(cfg)
        self.assertTrue(cfg["auth_checked"], "flaga pozwala skasować alert bez kandydatów")


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


class BrowserModeTokenTest(unittest.TestCase):
    """W trybie przeglądarki (scalony dodatek) monitor NIE robi /auth/refresh."""

    @staticmethod
    def fresh_jwt():
        return jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)

    @staticmethod
    def expired_jwt():
        return jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) - 10)

    def test_fresh_token_used_without_refresh(self):
        valid = self.fresh_jwt()
        cfg = {"token": valid, "browser_mode": True}
        with mock.patch.object(cp, "refresh_decathlon_token",
                               side_effect=AssertionError("w trybie przeglądarki nie wolno odświeżać")):
            token, err = cp.ensure_decathlon_token(cfg)
        self.assertIsNone(err)
        self.assertEqual(token, valid)

    def test_long_expired_token_asks_login(self):
        """Token martwy dłużej niż karencja -> błąd auth z instrukcją logowania."""
        expired = jwt_with_exp(
            int(datetime.now(timezone.utc).timestamp()) - cp.BROWSER_RENEW_GRACE - 60)
        cfg = {"token": expired, "browser_mode": True}
        with mock.patch.object(cp, "refresh_decathlon_token",
                               side_effect=AssertionError("w trybie przeglądarki nie wolno odświeżać")):
            token, err = cp.ensure_decathlon_token(cfg)
        self.assertEqual(token, expired)  # zwracamy jak jest — przeglądarka może właśnie odnawiać
        self.assertIn("panel", err.lower())
        self.assertTrue(any(m in err for m in cp.AUTH_FAILURE_MARKERS),
                        "musi być rozpoznane jako błąd auth (pending_ids + przerwanie przebiegu)")

    def test_freshly_expired_token_is_renewal_dip_not_alarm(self):
        """Dołek odnowy: świeżo wygasły token to norma (czytnik zaraz odnowi) — bez alarmu.

        Regresja: bez karencji każdy ~15-minutowy cykl odnowy wysyłał fałszywy push
        „token wygasł" w kilkunastosekundowym oknie między exp a zapisem świeżego pliku.
        """
        just_expired = self.expired_jwt()  # wygasł 10 s temu — głęboko w karencji
        cfg = {"token": just_expired, "browser_mode": True}
        with mock.patch.object(cp, "refresh_decathlon_token",
                               side_effect=AssertionError("w trybie przeglądarki nie wolno odświeżać")):
            token, err = cp.ensure_decathlon_token(cfg)
        self.assertIsNone(err)
        self.assertEqual(token, just_expired)

    def test_missing_token_asks_login(self):
        cfg = {"token": "", "browser_mode": True}
        with mock.patch.object(cp, "refresh_decathlon_token",
                               side_effect=AssertionError("w trybie przeglądarki nie wolno odświeżać")):
            token, err = cp.ensure_decathlon_token(cfg)
        self.assertIsNone(token)
        self.assertIn("panel", err.lower())
        self.assertTrue(any(m in err for m in cp.AUTH_FAILURE_MARKERS))

    def test_token_inside_margin_is_still_valid(self):
        """Strona odnawia token dopiero PO wygaśnięciu — token z <5 min życia MUSI działać.

        Regresja: margines TOKEN_EXPIRY_MARGIN (300 s) uznawał taki token za wygasły,
        co blokowało rejestrację ważnym tokenem przez ~1/3 jego życia.
        """
        soon = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 120)  # 2 min < margines
        cfg = {"token": soon, "browser_mode": True}
        with mock.patch.object(cp, "refresh_decathlon_token",
                               side_effect=AssertionError("w trybie przeglądarki nie wolno odświeżać")):
            token, err = cp.ensure_decathlon_token(cfg)
        self.assertIsNone(err)
        self.assertEqual(token, soon)

    def test_register_401_rereads_token_file(self):
        """Po HTTP 401 w trybie przeglądarki monitor czyta plik tokenu ponownie i ponawia."""
        seen = []

        def fake_rpc(method, token, payload):
            seen.append(token)
            if len(seen) == 1:
                raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
            return {"processState": "accepted"}

        now = int(datetime.now(timezone.utc).timestamp())
        fresh = jwt_with_exp(now + 3700)  # świeższy niż token w cfg — MUSI się różnić
        slot = {"id": "L:D", "listing_id": "L", "date_id": "D",
                "start_utc": datetime(2026, 7, 7, 10, 0, tzinfo=timezone.utc), "price": None}
        cfg = {"token": jwt_with_exp(now + 3600), "browser_mode": True,
               "name": "Jan Kowalski", "free_only": True}
        with mock.patch.object(cp, "decathlon_rpc", fake_rpc), \
                mock.patch.object(cp, "TOKEN_FILE", "/data/token.json"), \
                mock.patch.object(cp, "token_from_file", lambda: fresh), \
                mock.patch.object(cp, "refresh_decathlon_token",
                                  side_effect=AssertionError("w trybie przeglądarki nie wolno odświeżać")):
            ok, msg = cp.register_slot(slot, None, cfg, speculative=True)
        self.assertTrue(ok)
        self.assertEqual(seen[1], fresh, "druga próba musi iść świeżym tokenem z pliku")
        self.assertEqual(cfg["token"], fresh)

    def test_register_401_without_fresher_file_asks_login(self):
        """Gdy plik nie ma świeższego tokenu, błąd mówi o panelu i jest błędem auth."""
        same = self.fresh_jwt()

        def fake_rpc(method, token, payload):
            raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}"))

        slot = {"id": "L:D", "listing_id": "L", "date_id": "D",
                "start_utc": datetime(2026, 7, 7, 10, 0, tzinfo=timezone.utc), "price": None}
        cfg = {"token": same, "browser_mode": True, "name": "Jan Kowalski", "free_only": True}
        with mock.patch.object(cp, "decathlon_rpc", fake_rpc), \
                mock.patch.object(cp, "token_from_file", lambda: same), \
                mock.patch.object(cp.time, "sleep"), \
                mock.patch.object(cp, "refresh_decathlon_token",
                                  side_effect=AssertionError("w trybie przeglądarki nie wolno odświeżać")):
            ok, msg = cp.register_slot(slot, None, cfg, speculative=True)
        self.assertFalse(ok)
        self.assertIn("panel", msg.lower())
        self.assertTrue(any(m in msg for m in cp.AUTH_FAILURE_MARKERS))


class TokenFromFileTest(unittest.TestCase):
    """Token pisany przez przeglądarkę (scalony dodatek) i czytany przez monitor."""

    def test_reads_jwt_from_file(self):
        tok = jwt_with_exp(9999999999)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"jwt": tok, "exp": 9999999999}, f)
            path = f.name
        try:
            with mock.patch.object(cp, "TOKEN_FILE", path):
                self.assertEqual(cp.token_from_file(), tok)
        finally:
            os.unlink(path)

    def test_strips_prefixes(self):
        tok = jwt_with_exp(9999999999)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"jwt": f"Bearer {tok}"}, f)
            path = f.name
        try:
            with mock.patch.object(cp, "TOKEN_FILE", path):
                self.assertEqual(cp.token_from_file(), tok)
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty(self):
        with mock.patch.object(cp, "TOKEN_FILE", "/nonexistent/definitely/xyz.json"):
            self.assertEqual(cp.token_from_file(), "")

    def test_disabled_when_unset(self):
        with mock.patch.object(cp, "TOKEN_FILE", ""):
            self.assertEqual(cp.token_from_file(), "")

    def test_bad_json_returns_empty(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{ to nie jest json")
            path = f.name
        try:
            with mock.patch.object(cp, "TOKEN_FILE", path):
                self.assertEqual(cp.token_from_file(), "")
        finally:
            os.unlink(path)


class FakeHTTPResponse:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self._body = body
        self.headers = {} if headers is None else headers

    def read(self):
        return self._body


class FakeConnection:
    """Udaje http.client.HTTPSConnection: liczy wysłane zapytania i potrafi zerwać gniazdo."""

    created = 0
    script = []   # kolejka WSPÓLNA dla wszystkich połączeń — ponowienie ma iść dalej,
                  # a nie odtwarzać od nowa tego samego błędu

    def __init__(self, host, timeout=None):
        FakeConnection.created += 1
        self.host = host
        self.sent = []
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        self.sent.append((method, path, body, headers))
        if FakeConnection.script and isinstance(FakeConnection.script[0], Exception):
            raise FakeConnection.script.pop(0)

    def getresponse(self):
        item = FakeConnection.script.pop(0) if FakeConnection.script else FakeHTTPResponse()
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class OpenUrlTest(unittest.TestCase):
    """Warstwa transportu: podtrzymane połączenie zamiast nowego przy każdym zapytaniu."""

    def setUp(self):
        cp.drop_connection()
        FakeConnection.created = 0
        FakeConnection.script = []
        patcher = mock.patch.object(cp.http.client, "HTTPSConnection", FakeConnection)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(cp.drop_connection)

    def req(self, url="https://go.decathlon.pl/api/listing/X?include=dates", data=None, method=None):
        return urllib.request.Request(url, data=data, method=method,
                                      headers={"User-Agent": "test"})

    def test_reuses_one_connection_for_repeated_calls(self):
        FakeConnection.script = [FakeHTTPResponse(body=b'{"a":1}')] * 3
        for _ in range(3):
            with cp.open_url(self.req()) as resp:
                self.assertEqual(resp.read(), b'{"a":1}')
        self.assertEqual(FakeConnection.created, 1)   # sedno zmiany: JEDNO połączenie

    def test_path_and_method_are_passed_through(self):
        FakeConnection.script = [FakeHTTPResponse()]
        cp.open_url(self.req("https://h/api/v2/x", data=b"{}", method="POST"))
        method, path, body, _ = cp._conn_pool()["h"].sent[0]
        self.assertEqual((method, path, body), ("POST", "/api/v2/x", b"{}"))

    def test_query_string_survives(self):
        FakeConnection.script = [FakeHTTPResponse()]
        cp.open_url(self.req())
        self.assertEqual(cp._conn_pool()["go.decathlon.pl"].sent[0][1],
                         "/api/listing/X?include=dates")

    def test_dropped_socket_is_retried_on_a_fresh_connection(self):
        """Serwer zamyka bezczynne gniazdo — to normalne, nie może kosztować biegu."""
        FakeConnection.script = [ConnectionResetError("zerwane"), FakeHTTPResponse(body=b"ok")]
        with cp.open_url(self.req()) as resp:
            self.assertEqual(resp.read(), b"ok")
        self.assertEqual(FakeConnection.created, 2)

    def test_second_failure_is_a_network_error(self):
        FakeConnection.script = [ConnectionResetError("raz"), ConnectionResetError("dwa")]
        with self.assertRaises(urllib.error.URLError):
            cp.open_url(self.req())

    def test_http_error_keeps_urlopen_contract(self):
        FakeConnection.script = [FakeHTTPResponse(status=409, body=b'{"message":"No available seats"}')]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            cp.open_url(self.req())
        self.assertEqual(ctx.exception.code, 409)
        self.assertIn(b"No available seats", ctx.exception.read())

    def test_connection_close_header_drops_the_socket(self):
        FakeConnection.script = [FakeHTTPResponse(headers={"Connection": "close"}),
                                 FakeHTTPResponse()]
        cp.open_url(self.req())
        self.assertEqual(cp._conn_pool(), {})
        cp.open_url(self.req())
        self.assertEqual(FakeConnection.created, 2)

    def test_redirect_is_followed(self):
        FakeConnection.script = [FakeHTTPResponse(status=301, headers={"Location": "/nowy"}),
                                 FakeHTTPResponse(body=b"tam")]
        with cp.open_url(self.req()) as resp:
            self.assertEqual(resp.read(), b"tam")
        self.assertEqual(cp._conn_pool()["go.decathlon.pl"].sent[-1][1], "/nowy")

    def test_redirect_loop_gives_up(self):
        FakeConnection.script = [FakeHTTPResponse(status=301, headers={"Location": "/x"})] * 9
        with self.assertRaises(urllib.error.URLError):
            cp.open_url(self.req())


class SalvoHelpers:
    """Wspólne atrapy salwy. Bez TestCase, żeby testy bazowe nie biegły dwa razy."""

    def slots(self, *hours):
        return [{"id": f"s{h}", "date_id": f"d{h}", "name": "Rezerwacja godzinna",
                 "start_utc": datetime(2026, 8, 14, h, 0, tzinfo=timezone.utc),
                 "count": 0, "limit": 1, "price": None} for h in hours]

    def cfg(self, **over):
        base = {"enabled": True, "max_per_run": 1, "order": "latest", "salvo": 4,
                "name": "Jan", "browser_mode": True,
                "token": jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)}
        base.update(over)
        return base

    def run_with(self, outcomes, cfg=None, slots=None):
        """outcomes: {godzina: (ok, komunikat, id_transakcji)}"""
        self.cancelled = []
        self.threads = set()

        def fake_register(slot, price, local_cfg, speculative=False):
            self.threads.add(threading.current_thread().name)
            hour = slot["start_utc"].hour
            ok, msg, tx = outcomes[hour]
            if ok:
                local_cfg["transaction_id"] = tx
            return ok, msg

        def fake_cancel(tx, _cfg):
            self.cancelled.append(tx)
            return True, "cancelled"

        buf = io.StringIO()
        with mock.patch.object(cp, "register_slot", side_effect=fake_register), \
                mock.patch.object(cp, "cancel_reservation", side_effect=fake_cancel), \
                mock.patch("sys.stdout", buf):
            res, reg = cp.auto_register_new_slots(
                slots or self.slots(15, 17, 18, 19), {}, cfg or self.cfg(), set())
        return res, reg, buf.getvalue()

class SalvoTest(SalvoHelpers, unittest.TestCase):
    """Salwa: równoległe strzały, limit nadal obowiązuje, nadmiar wraca do puli."""

    def test_attempts_really_run_in_parallel(self):
        """Sedno zmiany: cztery strzały muszą lecieć NARAZ.

        Bariera jest dowodem rozstrzygającym — przy wykonaniu po kolei pierwszy strzał
        czekałby w nieskończoność na pozostałe i test padłby na timeout.
        """
        barrier = threading.Barrier(4, timeout=5)

        def fake_register(slot, price, local_cfg, speculative=False):
            barrier.wait()          # przejdzie tylko, gdy wszystkie cztery są w locie
            return False, "409"

        with mock.patch.object(cp, "register_slot", side_effect=fake_register), \
                mock.patch("sys.stdout", io.StringIO()):
            cp.auto_register_new_slots(self.slots(15, 17, 18, 19), {}, self.cfg(), set())

    def test_first_preference_wins_and_rest_is_cancelled(self):
        # wszystkie cztery przechodzą: zostaje 19:00 (order=latest), reszta wraca
        res, reg, out = self.run_with({h: (True, "accepted", f"tx{h}") for h in (15, 17, 18, 19)})
        self.assertEqual(sorted(self.cancelled), ["tx15", "tx17", "tx18"])
        self.assertEqual(res["s19"], (True, "accepted"))
        self.assertIn("s19", reg)
        self.assertIn("↩ Salwa", out)

    def test_limit_two_keeps_two(self):
        cfg = self.cfg(max_per_run=2)
        res, reg, _ = self.run_with({h: (True, "accepted", f"tx{h}") for h in (15, 17, 18, 19)}, cfg)
        self.assertEqual(sorted(self.cancelled), ["tx15", "tx17"])
        self.assertEqual([r for r in ("s19", "s18") if r in reg], ["s19", "s18"])

    def test_cancelled_extras_are_not_retried_later(self):
        _, reg, _ = self.run_with({h: (True, "accepted", f"tx{h}") for h in (15, 17, 18, 19)})
        # także te oddane trafiają do „zarejestrowanych", żeby nie wpaść w pętlę
        # rezerwuj–anuluj–zobacz-wolne–rezerwuj
        self.assertEqual(reg, {"s15", "s17", "s18", "s19"})

    def test_all_failed_falls_through_to_the_rest(self):
        slots = self.slots(15, 16, 17, 18, 19)          # 5 terminów, salwa bierze 4
        res, reg, out = self.run_with(
            {19: (False, "409", ""), 18: (False, "409", ""), 17: (False, "409", ""),
             16: (False, "409", ""), 15: (True, "accepted", "tx15")},
            slots=slots)
        self.assertEqual(res["s15"], (True, "accepted"))   # ostatni poszedł sekwencyjnie
        self.assertIn("s15", reg)

    def test_auth_failure_aborts_and_remembers(self):
        cfg = self.cfg()
        res, reg, out = self.run_with(
            {19: (False, "token odrzucony (HTTP 401) — zaloguj się w panelu Padel", ""),
             18: (False, "409", ""), 17: (False, "409", ""), 15: (False, "409", "")}, cfg)
        self.assertIn("Auto-rejestracja przerwana", out)
        self.assertTrue(cfg["pending_ids"])

    def test_speculative_never_cancels_or_registers(self):
        cfg = self.cfg(speculative=True)
        res, reg, out = self.run_with({h: (True, "walidacja OK", f"tx{h}") for h in (15, 17, 18, 19)}, cfg)
        self.assertEqual(self.cancelled, [])
        self.assertEqual(reg, set())

    def test_disabled_salvo_keeps_sequential_path(self):
        cfg = self.cfg(salvo=0)
        self.run_with({19: (True, "accepted", "tx19"), 18: (False, "409", ""),
                       17: (False, "409", ""), 15: (False, "409", "")}, cfg)
        self.assertEqual(len(self.threads), 1)   # bez salwy wszystko w wątku głównym


class SingleSlotSalvoTest(SalvoHelpers, unittest.TestCase):
    """REGRESJA 9.08: samotny termin szedł z wątku GŁÓWNEGO, poza rozgrzaną pulą.

    Tego dnia 19:00 przyszedł w osobnej partii, sam. Strzał kosztował 319 ms — wobec
    57–84 ms strzałów z puli salwy w tej samej sekundzie — i termin przepadł na 409.
    """

    def test_single_slot_uses_salvo_pool(self):
        res, _, out = self.run_with({19: (True, "accepted", "tx19")},
                                    slots=self.slots(19))
        self.assertTrue(res["s19"][0])
        self.assertEqual(len(self.threads), 1)
        self.assertTrue(next(iter(self.threads)).startswith("salwa"),
                        f"strzał poszedł spoza puli salwy: {self.threads}")

    def test_single_slot_log_is_not_called_a_salvo(self):
        """„Salwa: 1 prób równolegle" to bełkot — jeden strzał to nie salwa."""
        _, _, out = self.run_with({19: (True, "accepted", "tx19")}, slots=self.slots(19))
        self.assertIn("Strzał z rozgrzanego wątku", out)
        self.assertNotIn("1 prób równolegle", out)

    def test_salvo_disabled_still_runs_sequentially(self):
        """Przy wyłączonej salwie nic się nie zmienia — strzał z wątku wywołującego."""
        res, _, _ = self.run_with({19: (True, "accepted", "tx19")},
                                  cfg=self.cfg(salvo=0), slots=self.slots(19))
        self.assertTrue(res["s19"][0])
        self.assertEqual(self.threads, {threading.current_thread().name})


class SalvoRobustnessTest(SalvoHelpers, unittest.TestCase):
    """Awarie w trakcie salwy nie mogą zostawić zajętego kortu bez śladu."""

    def test_worker_exception_does_not_kill_the_salvo(self):
        """Jeden zepsuty JSON nie może wywrócić całej salwy.

        pool.map podnosi wyjątek dopiero przy odczycie wyników — bez osłony
        rezerwacje zrobione przez pozostałe wątki zostałyby nieobsłużone:
        ani zapisane, ani anulowane, ani zgłoszone w powiadomieniu.
        """
        def fake_register(slot, price, local_cfg, speculative=False):
            if slot["start_utc"].hour == 18:
                raise ValueError("zepsuty JSON")
            local_cfg["transaction_id"] = f"tx{slot['start_utc'].hour}"
            return True, "accepted"

        self.cancelled = []
        buf = io.StringIO()
        with mock.patch.object(cp, "register_slot", side_effect=fake_register), \
                mock.patch.object(cp, "cancel_reservation",
                                  side_effect=lambda tx, c: (self.cancelled.append(tx),
                                                             (True, "cancelled"))[1]), \
                mock.patch("sys.stdout", buf):
            res, reg = cp.auto_register_new_slots(
                self.slots(15, 17, 18, 19), {}, self.cfg(), set())
        self.assertEqual(res["s19"], (True, "accepted"))     # najlepszy zatrzymany
        self.assertEqual(sorted(self.cancelled), ["tx15", "tx17"])   # nadmiar oddany
        self.assertFalse(res["s18"][0])
        self.assertIn("nieoczekiwany błąd", res["s18"][1])

    def test_failed_cancellation_is_reported_not_hidden(self):
        """Gdy anulowanie padnie, użytkownik MUSI się dowiedzieć — kort zostaje zajęty."""
        def fake_cancel(tx, _cfg):
            return False, "anulowanie: Decathlon HTTP 500"

        buf = io.StringIO()
        with mock.patch.object(cp, "register_slot",
                               side_effect=lambda s, p, c, speculative=False:
                               (c.__setitem__("transaction_id", f"tx{s['start_utc'].hour}"),
                                (True, "accepted"))[1]), \
                mock.patch.object(cp, "cancel_reservation", side_effect=fake_cancel), \
                mock.patch("sys.stdout", buf):
            res, _ = cp.auto_register_new_slots(
                self.slots(15, 17, 18, 19), {}, self.cfg(), set())
        self.assertIn("NIE anulowano", res["s15"][1])
        self.assertIn("anuluj ręcznie", buf.getvalue())
        # ↩ to znacznik UDANEGO oddania terminu — przy nieudanym anulowaniu
        # nie może się pojawić ani razu, bo sugerowałby, że kort jest wolny
        self.assertNotIn("↩", buf.getvalue())

    def test_missing_transaction_id_is_loud(self):
        """Rezerwacja bez ID transakcji: nie ma czego anulować, więc trzeba krzyknąć."""
        buf = io.StringIO()
        with mock.patch.object(cp, "register_slot",
                               side_effect=lambda s, p, c, speculative=False: (True, "accepted")), \
                mock.patch.object(cp, "cancel_reservation",
                                  side_effect=AssertionError("nie ma czego anulować")), \
                mock.patch("sys.stdout", buf):
            res, reg = cp.auto_register_new_slots(
                self.slots(15, 17, 18, 19), {}, self.cfg(), set())
        self.assertIn("brak ID transakcji", res["s15"][1])
        self.assertIn("anuluj ręcznie w panelu Padel", buf.getvalue())
        self.assertIn("s15", reg)   # i tak nie próbujemy go ponownie

    def test_token_refreshed_in_a_worker_is_adopted(self):
        """Token odnowiony w wątku musi trafić do cfg — inaczej próby po salwie
        czekałyby drugi raz na to samo odnowienie (do ~24 s w gorącym oknie)."""
        now = int(datetime.now(timezone.utc).timestamp())
        swiezy = jwt_with_exp(now + 9000)
        cfg = self.cfg(token=jwt_with_exp(now + 3600))

        def fake_register(slot, price, local_cfg, speculative=False):
            if slot["start_utc"].hour == 19:
                local_cfg["token"] = swiezy      # ten wątek odnowił token po 401
            return False, "409"

        with mock.patch.object(cp, "register_slot", side_effect=fake_register), \
                mock.patch("sys.stdout", io.StringIO()):
            cp.auto_register_new_slots(self.slots(15, 17, 18, 19), {}, cfg, set())
        self.assertEqual(cfg["token"], swiezy)


class MeasureRttTest(unittest.TestCase):
    """Pomiar rundy do serwera — diagnostyka, więc nie może niczego wywrócić."""

    def gniazda(self, czasy):
        """Atrapa socket.socket: każde połączenie 'trwa' tyle, ile podano (None = błąd)."""
        kolejka = list(czasy)
        zegar = [0.0]

        class Gniazdo:
            def settimeout(self, _): pass
            def close(self): pass

            def connect(self, _adres):
                ile = kolejka.pop(0)
                if ile is None:
                    raise OSError("brak połączenia")
                zegar[0] += ile / 1000.0

        return Gniazdo, zegar

    def zmierz(self, czasy):
        Gniazdo, zegar = self.gniazda(czasy)
        with mock.patch.object(cp.socket, "socket", Gniazdo), \
                mock.patch.object(cp.time, "monotonic", side_effect=lambda: zegar[0]):
            return cp.measure_rtt("host", samples=len(czasy))

    def test_returns_median_min_max(self):
        # zaokrąglamy: sztuczny zegar sumuje ułamki zmiennoprzecinkowe
        self.assertEqual([round(x) for x in self.zmierz([50, 30, 40, 90, 60])], [50, 30, 90])

    def test_ignores_failed_attempts(self):
        self.assertEqual([round(x) for x in self.zmierz([None, 40, None, 60, 50])], [50, 40, 60])

    def test_all_failures_give_none(self):
        self.assertIsNone(self.zmierz([None, None, None]))

    def test_log_survives_unreachable_host(self):
        buf = io.StringIO()
        with mock.patch.object(cp, "measure_rtt", return_value=None), \
                mock.patch("sys.stdout", buf):
            self.assertIsNone(cp.log_rtt("host"))
        self.assertIn("Nie zmierzyłem opóźnienia", buf.getvalue())

    def test_high_latency_suggests_the_cable(self):
        buf = io.StringIO()
        with mock.patch.object(cp, "measure_rtt", return_value=(120, 110, 140)), \
                mock.patch("sys.stdout", buf):
            cp.log_rtt("host")
        out = buf.getvalue()
        self.assertIn("240 ms", out)      # dwie rundy w ścieżce rezerwacji
        self.assertIn("kabel", out)

    def test_good_latency_says_nothing_to_fix(self):
        buf = io.StringIO()
        with mock.patch.object(cp, "measure_rtt", return_value=(20, 18, 25)), \
                mock.patch("sys.stdout", buf):
            cp.log_rtt("host")
        self.assertIn("nie ma już czego poprawiać", buf.getvalue())
        self.assertNotIn("kabel", buf.getvalue())


class SprintTest(unittest.TestCase):
    """Sprint: ciągłe pobieranie aż do pojawienia się terminu spoza punktu odniesienia."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"FILTERS": "mon-sun:00:00-24:00"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def slot(self, sid):
        return {"id": sid, "date_id": f"d{sid}",
                "start_utc": datetime(2026, 8, 14, 19, 0, tzinfo=timezone.utc)}

    def sprint(self, kolejne_odpowiedzi, baseline, threads=2, sekundy=1.5):
        """kolejne_odpowiedzi: lista zbiorów ID zwracanych przez kolejne pobrania."""
        kolejka = list(kolejne_odpowiedzi)
        lock = threading.Lock()
        self.pobran = 0

        def fake_fetch(lid):
            with lock:
                self.pobran += 1
                ids = kolejka.pop(0) if kolejka else kolejne_odpowiedzi[-1]
            return {"__ids": ids}

        with mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: "kort"), \
                mock.patch.object(cp, "fetch_listing", side_effect=fake_fetch), \
                mock.patch.object(cp, "free_slots",
                                  side_effect=lambda doc, lid, now: [self.slot(i)
                                                                     for i in doc["__ids"]]), \
                mock.patch.object(cp, "passes_filter", return_value=True), \
                mock.patch("sys.stdout", io.StringIO()):
            return cp.run_sprint(time.monotonic() + sekundy, threads,
                                 "https://go.decathlon.pl/l/1c0ec93e-ca77-44b9-a3a6-c72a99d050dd", set(baseline), TZ)

    def test_returns_the_document_when_a_new_slot_appears(self):
        przed = time.monotonic()
        hit = self.sprint([{"a"}, {"a"}, {"a", "b"}], baseline={"a"})
        self.assertIsNotNone(hit)
        lid, doc, zobaczone = hit
        self.assertEqual(lid, "kort")
        self.assertIn("b", doc["__ids"])     # dane oddane BEZ ponownego pobierania
        # Trzeci element to punkt zerowy wieku danych — bez niego rejestracja nie wie,
        # jak stary był grafik, na podstawie którego strzela.
        self.assertGreaterEqual(zobaczone, przed)
        self.assertLessEqual(zobaczone, time.monotonic())

    def test_nothing_new_means_no_handoff(self):
        self.assertIsNone(self.sprint([{"a", "b"}], baseline={"a", "b"}, sekundy=0.8))

    def test_baseline_comes_from_saved_state_not_first_fetch(self):
        """Publikacja w pierwszych milisekundach sprintu MUSI zostać wykryta.

        Gdyby punkt odniesienia brał się z pierwszego pobrania, nowe terminy
        wpadłyby do niego i sprint nigdy by się nie odpalił.
        """
        hit = self.sprint([{"a", "nowy"}], baseline={"a"})   # nowe JUŻ w pierwszym pobraniu
        self.assertIsNotNone(hit)

    def test_survives_fetch_errors(self):
        kolejka = [ValueError("padlo"), {"a"}, {"a", "b"}]
        lock = threading.Lock()

        def fake_fetch(lid):
            with lock:
                item = kolejka.pop(0) if kolejka else {"a", "b"}
            if isinstance(item, Exception):
                raise item
            return {"__ids": item}

        with mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: "kort"), \
                mock.patch.object(cp, "fetch_listing", side_effect=fake_fetch), \
                mock.patch.object(cp, "free_slots",
                                  side_effect=lambda doc, lid, now: [self.slot(i)
                                                                     for i in doc["__ids"]]), \
                mock.patch.object(cp, "passes_filter", return_value=True), \
                mock.patch("sys.stdout", io.StringIO()):
            hit = cp.run_sprint(time.monotonic() + 1.5, 1,
                                "https://go.decathlon.pl/l/1c0ec93e-ca77-44b9-a3a6-c72a99d050dd", {"a"}, TZ)
        self.assertIsNotNone(hit)   # pojedyncza wpadka nie kończy sprintu


class SprintPoolTest(unittest.TestCase):
    """Maruder z poprzedniej rundy nie może zwęzić następnej."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"FILTERS": "mon-sun:00:00-24:00"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_slow_straggler_does_not_narrow_the_next_sprint(self):
        """Zwycięzca wraca od razu, więc maruderzy wciąż pobierają, gdy rusza kolejna
        runda. Gdyby pula miała dokładnie tyle miejsc, ile wątków, druga runda
        pobierałaby węższym frontem, niż prosił użytkownik — i to po cichu."""
        lock = threading.Lock()
        runda = [0]
        aktywne = {1: set(), 2: set()}
        szczyt = {1: 0, 2: 0}

        def fetch(lid):
            # Wątek liczy się do rundy, w której WYSTARTOWAŁ — inaczej maruder
            # z rundy 1 zawyżałby wynik rundy 2 i test mierzyłby bzdurę.
            moja = runda[0]
            nazwa = threading.current_thread().name
            with lock:
                aktywne[moja].add(nazwa)
                szczyt[moja] = max(szczyt[moja], len(aktywne[moja]))
            wolny = moja == 1 and nazwa.endswith("_0")
            time.sleep(0.35 if wolny else 0.05)
            with lock:
                aktywne[moja].discard(nazwa)
            return {"__ids": {"a", "nowy"}}

        slot = lambda i: {"id": i, "date_id": "d",
                          "start_utc": datetime(2026, 8, 14, 19, 0, tzinfo=timezone.utc)}
        with mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: "kort"), \
                mock.patch.object(cp, "fetch_listing", side_effect=fetch), \
                mock.patch.object(cp, "free_slots",
                                  side_effect=lambda d, l, n: [slot(i) for i in d["__ids"]]), \
                mock.patch.object(cp, "passes_filter", return_value=True), \
                mock.patch("sys.stdout", io.StringIO()):
            for nr in (1, 2):
                runda[0] = nr
                cp.run_sprint(time.monotonic() + 2.0, cp.SPRINT_MAX_THREADS,
                              "https://go.decathlon.pl/l/1c0ec93e-ca77-44b9-a3a6-c72a99d050dd",
                              {"a"}, TZ)
        self.assertEqual(szczyt[2], cp.SPRINT_MAX_THREADS,
                         f"druga runda pobierała {szczyt[2]} wątkami zamiast "
                         f"{cp.SPRINT_MAX_THREADS} — maruder zabrał miejsce w puli")


class PrefetchedTest(unittest.TestCase):
    """Dane ze sprintu muszą trafić prosto do rejestracji — bez drugiej rundy do serwera."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.object(cp, "STATE_PATH", os.path.join(self.dir.name, "s.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = mock.patch.dict(os.environ, {
            "LISTINGS": "https://go.decathlon.pl/l/" + "a" * 8 + "-1111-2222-3333-444444444444",
            "NTFY_TOPIC": "", "FILTERS": "mon-sun:00:00-24:00", "AUTO_REGISTER": "false",
            "CONFIG_PATH": os.path.join(self.dir.name, "brak.json"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def doc(self):
        return {"data": {"attributes": {"title": "Kort", "price": None,
                                        "datesStats": {"availableListingDates": 0}}},
                "included": []}

    def test_prefetched_document_skips_the_fetch(self):
        lid = "aaaaaaaa-1111-2222-3333-444444444444"
        with mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: lid), \
                mock.patch.object(cp, "fetch_listing") as heavy, \
                mock.patch.object(cp, "fetch_listing_light") as light, \
                mock.patch("sys.stdout", io.StringIO()):
            cp.run_once(skip_light=True, prefetched=(lid, self.doc()))
        heavy.assert_not_called()
        light.assert_not_called()

    def test_document_for_another_court_is_ignored(self):
        lid = "aaaaaaaa-1111-2222-3333-444444444444"
        with mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: lid), \
                mock.patch.object(cp, "fetch_listing", return_value=self.doc()) as heavy, \
                mock.patch("sys.stdout", io.StringIO()):
            cp.run_once(skip_light=True, prefetched=("inny-kort", self.doc()))
        heavy.assert_called_once()   # nie podstawiamy danych z innego kortu


class WarmSalvoTest(unittest.TestCase):
    def test_warms_distinct_connections(self):
        """Bariera musi rozdzielić rozgrzewkę na osobne wątki — inaczej grzejemy jedno."""
        seen = set()

        def fake_open(req, timeout=30):
            seen.add(threading.current_thread().name)
            return io.BytesIO(b"{}")

        with mock.patch.object(cp, "open_url", side_effect=fake_open):
            cp.warm_connections(cp.salvo_pool(4), 4, "https://go.decathlon.pl/api/listing/X")
        self.assertEqual(len(seen), 4, f"rozgrzano tylko {len(seen)} połączeń")


class LogPrecisionTest(unittest.TestCase):
    """Milisekundy w Dzienniku tylko na czas zrywu — poza nim byłyby szumem."""

    def setUp(self):
        self.addCleanup(cp.set_log_precision, False)

    def zapisz(self):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            cp.log("test")
        return buf.getvalue()

    def test_default_is_seconds(self):
        cp.set_log_precision(False)
        self.assertRegex(self.zapisz(), r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] test")

    def test_burst_adds_milliseconds(self):
        cp.set_log_precision(True)
        self.assertRegex(self.zapisz(), r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\] test")

    def test_toggles_back(self):
        cp.set_log_precision(True)
        cp.set_log_precision(False)
        self.assertNotIn(".", self.zapisz().split("]")[0].split(" ")[-1])


class AttemptTimingTest(unittest.TestCase):
    """Czas każdej próby rejestracji w logu — mówi, ile kosztuje nieudany strzał."""

    def slots(self, *hours):
        return [{"id": f"s{h}", "date_id": f"d{h}", "name": "Rezerwacja godzinna",
                 "start_utc": datetime(2026, 8, 14, h, 0, tzinfo=timezone.utc),
                 "count": 0, "limit": 1, "price": None} for h in hours]

    def run_register(self, outcomes):
        cfg = {"enabled": True, "max_per_run": 1, "order": "latest", "name": "Jan",
               "token": jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)}
        calls = iter(outcomes)

        def fake_register(slot, price, cfg_, speculative=False):
            return next(calls)

        buf = io.StringIO()
        with mock.patch.object(cp, "register_slot", side_effect=fake_register), \
                mock.patch("sys.stdout", buf):
            cp.auto_register_new_slots(self.slots(15, 19), {}, cfg, set())
        return buf.getvalue()

    def test_failure_line_reports_duration(self):
        out = self.run_register([(False, "Decathlon HTTP 409: brak miejsc"), (True, "accepted")])
        self.assertRegex(out, r"! Auto-rejestracja nieudana .*409.* \[\d+ ms\]")

    def test_success_line_reports_duration(self):
        out = self.run_register([(True, "accepted")])
        self.assertRegex(out, r"✓ Auto-rejestracja: .* accepted \[\d+ ms\]")


class FetchTimingTest(unittest.TestCase):
    """Czas pobrania dopisujemy tylko w zrywie — oddziela sieć od reszty opóźnienia."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.object(cp, "STATE_PATH", os.path.join(self.dir.name, "s.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = mock.patch.dict(os.environ, {
            "LISTINGS": "https://go.decathlon.pl/l/" + "a" * 8 + "-1111-2222-3333-444444444444",
            "NTFY_TOPIC": "", "FILTERS": "mon-sun:00:00-24:00", "AUTO_REGISTER": "false",
            "CONFIG_PATH": os.path.join(self.dir.name, "brak.json"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def output(self, skip_light):
        doc = {"data": {"attributes": {"title": "Kort", "price": None,
                                       "datesStats": {"availableListingDates": 1}}},
               "included": []}
        buf = io.StringIO()
        with mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: x), \
                mock.patch.object(cp, "fetch_listing_light", return_value=doc), \
                mock.patch.object(cp, "fetch_listing", return_value=doc), \
                mock.patch("sys.stdout", buf):
            cp.run_once(skip_light=skip_light)
        return buf.getvalue()

    def test_burst_run_shows_fetch_time(self):
        self.assertRegex(self.output(True), r"pasujących do filtra \(pobranie \d+ ms\)")

    def test_normal_run_keeps_the_line_clean(self):
        self.assertNotIn("pobranie", self.output(False))


class ParseBurstTest(unittest.TestCase):
    def test_full_time(self):
        got = cp.parse_burst_env("mon-sun:11:00:45")
        self.assertEqual(got["at"], (11, 0, 45))
        self.assertEqual(len(got["days"]), 7)

    def test_seconds_optional(self):
        self.assertEqual(cp.parse_burst_env("mon-fri:11:00")["at"], (11, 0, 0))

    def test_day_list(self):
        self.assertEqual(cp.parse_burst_env("mon,wed:11:00:45")["days"], ["mon", "wed"])

    def test_empty_means_disabled(self):
        self.assertIsNone(cp.parse_burst_env(""))
        self.assertIsNone(cp.parse_burst_env("   "))

    def test_garbage_raises(self):
        for spec in ("mon-sun", "mon-sun:", "mon-sun:25:00:00", "mon-sun:aa:bb", "mon-sun:11:00:99"):
            with self.assertRaises(ValueError, msg=spec):
                cp.parse_burst_env(spec)


class BurstBoundsTest(unittest.TestCase):
    def burst(self, days="mon-sun", at="11:00:45", seconds=15, interval=0.5):
        cfg = cp.parse_burst_env(f"{days}:{at}")
        cfg.update(seconds=seconds, interval=interval)
        return cfg

    def at(self, *args):
        return datetime(*args, tzinfo=TZ)

    def test_before_burst_returns_todays_window(self):
        start, end = cp.burst_bounds(self.burst(), TZ, self.at(2026, 8, 5, 10, 0))
        self.assertEqual((start.hour, start.minute, start.second), (11, 0, 45))
        self.assertEqual((end - start).total_seconds(), 15)

    def test_inside_burst_returns_the_running_window(self):
        bounds = cp.burst_bounds(self.burst(), TZ, self.at(2026, 8, 5, 11, 0, 50))
        self.assertTrue(bounds[0] <= self.at(2026, 8, 5, 11, 0, 50) < bounds[1])

    def test_after_burst_jumps_to_next_day(self):
        start, _ = cp.burst_bounds(self.burst(), TZ, self.at(2026, 8, 5, 12, 0))
        self.assertEqual(start.day, 6)

    def test_skips_days_outside_the_list(self):
        # 2026-08-05 to środa; przy 'sat' najbliższy zryw wypada w sobotę 08.08
        start, _ = cp.burst_bounds(self.burst(days="sat"), TZ, self.at(2026, 8, 5, 12, 0))
        self.assertEqual((start.day, start.weekday()), (8, 5))

    def test_disabled_burst(self):
        self.assertIsNone(cp.burst_bounds(None, TZ, self.at(2026, 8, 5, 11, 0)))


class PlanSleepTest(unittest.TestCase):
    def burst(self, seconds=15, interval=0.5):
        cfg = cp.parse_burst_env("mon-sun:11:00:45")
        cfg.update(seconds=seconds, interval=interval)
        return cfg

    def at(self, *args):
        return datetime(*args, tzinfo=TZ)

    def test_work_time_is_subtracted_from_the_wait(self):
        """Ustawione 2s musi znaczyć 2s, a nie 2s + czas zapytań."""
        got = cp.plan_sleep(2, [], None, TZ, elapsed=0.4, now=self.at(2026, 8, 5, 9, 0))
        self.assertAlmostEqual(got, 1.6, places=3)

    def test_never_returns_a_busy_loop(self):
        got = cp.plan_sleep(2, [], None, TZ, elapsed=99, now=self.at(2026, 8, 5, 9, 0))
        self.assertEqual(got, cp.MIN_SLEEP_SECONDS)

    def test_inside_burst_uses_the_burst_tempo(self):
        got = cp.plan_sleep(60, [], self.burst(), TZ, elapsed=0.1,
                            now=self.at(2026, 8, 5, 11, 0, 50))
        self.assertAlmostEqual(got, 0.4, places=3)

    def test_sleeps_exactly_up_to_the_burst_start(self):
        """Bez tego zwykły takt przespałby moment publikacji."""
        got = cp.plan_sleep(60, [], self.burst(), TZ, elapsed=0,
                            now=self.at(2026, 8, 5, 11, 0, 20))
        self.assertAlmostEqual(got, 25, places=3)

    def test_lands_on_the_burst_start_even_after_slow_work(self):
        """Czasu pracy NIE odejmujemy od dosypiania do zrywu — inaczej budzimy się obok celu."""
        got = cp.plan_sleep(60, [], self.burst(), TZ, elapsed=0.4,
                            now=self.at(2026, 8, 5, 11, 0, 40))
        self.assertAlmostEqual(got, 5.0, places=3)   # 11:00:40 + 5 s = 11:00:45 co do sekundy

    def test_far_from_burst_uses_the_normal_interval(self):
        got = cp.plan_sleep(30, [], self.burst(), TZ, elapsed=0,
                            now=self.at(2026, 8, 5, 3, 0))
        self.assertAlmostEqual(got, 30, places=3)

    def test_interval_windows_still_apply(self):
        windows = cp.parse_intervals_env("mon-sun:08:00-12:00=7")
        got = cp.plan_sleep(60, windows, None, TZ, elapsed=0, now=self.at(2026, 8, 5, 9, 0))
        self.assertAlmostEqual(got, 7, places=3)


class SkipLightPingTest(unittest.TestCase):
    """W zrywie pomijamy lekki ping — pełne dane niosą te same atrybuty kortu."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.object(cp, "STATE_PATH", os.path.join(self.dir.name, "s.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = mock.patch.dict(os.environ, {
            "LISTINGS": "https://go.decathlon.pl/l/" + "a" * 8 + "-1111-2222-3333-444444444444",
            "NTFY_TOPIC": "", "FILTERS": "mon-sun:00:00-24:00", "AUTO_REGISTER": "false",
            "CONFIG_PATH": os.path.join(self.dir.name, "brak.json"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def heavy(self):
        return {"data": {"attributes": {"title": "Kort", "price": None,
                                        "datesStats": {"availableListingDates": 0}}},
                "included": []}

    def run_with(self, skip_light):
        with mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: x), \
                mock.patch.object(cp, "fetch_listing_light", return_value=self.heavy()) as light, \
                mock.patch.object(cp, "fetch_listing", return_value=self.heavy()) as heavy:
            cp.run_once(skip_light=skip_light)
        return light, heavy

    def test_burst_goes_straight_for_the_full_payload(self):
        light, heavy = self.run_with(True)
        light.assert_not_called()
        heavy.assert_called_once()

    def test_normal_run_still_starts_with_the_light_ping(self):
        light, heavy = self.run_with(False)
        light.assert_called_once()
        heavy.assert_not_called()      # 0 dostępnych -> pełne dane niepotrzebne


def transaction(date="2026-08-04T18:00:00.000+00:00", duration=60, state="accepted", **over):
    """Transakcja w kształcie, jaki zwraca transactions.list z rozszerzeniami."""
    doc = {
        "id": "tx-1",
        "processState": state,
        "listingDate": {"date": date, "duration": duration, "name": "Rezerwacja godzinna",
                        "cancelled": False},
        "listing": {"id": "court-1", "title": "Kort do Padla — Targówek",
                    "location": {"address": "Geodezyjna 76, Warszawa"}},
        "participants": [{"name": "Jan Kowalski"}],
    }
    doc.update(over)
    return doc


class NormalizeReservationTest(unittest.TestCase):
    def test_flat_shape(self):
        res = cp.normalize_reservation(transaction())
        self.assertEqual(res["id"], "tx-1")
        self.assertEqual(res["minutes"], 60)
        self.assertEqual(res["title"], "Kort do Padla — Targówek")
        self.assertEqual(res["address"], "Geodezyjna 76, Warszawa")
        self.assertEqual(res["participants"], ["Jan Kowalski"])
        self.assertFalse(res["cancelled"])

    def test_jsonapi_shape_with_attributes(self):
        """Starsze endpointy pakują pola w 'attributes', a id w {'uuid': …}."""
        raw = {
            "id": {"uuid": "tx-9"},
            "attributes": {"processState": "accepted"},
            "listingDate": {"attributes": {"date": "2026-08-04T18:00:00+00:00", "duration": 90}},
            "listing": {"attributes": {"title": "Kort"}},
        }
        # relacje leżą obok 'attributes' — _flat scala je do jednego widoku
        raw["attributes"]["listingDate"] = raw.pop("listingDate")
        raw["attributes"]["listing"] = raw.pop("listing")
        res = cp.normalize_reservation(raw)
        self.assertEqual(res["id"], "tx-9")
        self.assertEqual(res["minutes"], 90)
        self.assertEqual(res["title"], "Kort")

    def test_missing_duration_falls_back_to_hour(self):
        res = cp.normalize_reservation(transaction(duration=None))
        self.assertEqual(res["minutes"], 60)

    def test_cancelled_state_detected(self):
        self.assertTrue(cp.normalize_reservation(transaction(state="cancelled"))["cancelled"])
        self.assertTrue(cp.normalize_reservation(transaction(state="payment-expired"))["cancelled"])

    def test_cancelled_date_marks_reservation(self):
        raw = transaction()
        raw["listingDate"]["cancelled"] = True
        self.assertTrue(cp.normalize_reservation(raw)["cancelled"])

    def test_unknown_state_stays_active(self):
        self.assertFalse(cp.normalize_reservation(transaction(state="jakiś-nowy-stan"))["cancelled"])

    def test_broken_date_does_not_raise(self):
        res = cp.normalize_reservation(transaction(date="nie-data"))
        self.assertIsNone(res["start_utc"])

    def test_past_flag(self):
        now = datetime(2026, 8, 5, tzinfo=timezone.utc)
        self.assertTrue(cp.normalize_reservation(transaction(), now)["past"])
        self.assertFalse(cp.normalize_reservation(transaction(), now - timedelta(days=5))["past"])


class ReservationsViewTest(unittest.TestCase):
    def view(self, items, now):
        with mock.patch.object(cp, "fetch_my_reservations", return_value=(items, None)):
            return cp.reservations_view({}, TZ, now)

    def test_formats_local_hours(self):
        items, error = self.view([transaction()], datetime(2026, 8, 1, tzinfo=timezone.utc))
        self.assertIsNone(error)
        # 18:00 UTC = 20:00 w Warszawie (czas letni), termin godzinny
        self.assertEqual(items[0]["hours"], "20:00–21:00")
        self.assertEqual(items[0]["day"], "04.08.2026")
        self.assertIn("court-1", items[0]["book_url"])

    def test_skips_transaction_without_date(self):
        raw = transaction()
        raw["listingDate"] = {}
        items, _ = self.view([raw], datetime(2026, 8, 1, tzinfo=timezone.utc))
        self.assertEqual(items, [])

    def test_upcoming_ascending_past_descending(self):
        now = datetime(2026, 8, 10, tzinfo=timezone.utc)
        raws = [
            transaction(id="past-old", date="2026-08-01T10:00:00+00:00"),
            transaction(id="next-late", date="2026-08-20T10:00:00+00:00"),
            transaction(id="past-recent", date="2026-08-09T10:00:00+00:00"),
            transaction(id="next-soon", date="2026-08-12T10:00:00+00:00"),
        ]
        items, _ = self.view(raws, now)
        self.assertEqual([i["id"] for i in items],
                         ["next-soon", "next-late", "past-recent", "past-old"])

    def test_error_is_passed_through(self):
        with mock.patch.object(cp, "fetch_my_reservations", return_value=(None, "brak tokenu")):
            items, error = cp.reservations_view({}, TZ)
        self.assertIsNone(items)
        self.assertEqual(error, "brak tokenu")


class FetchMyReservationsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {"token": jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)}

    def test_paginates_and_deduplicates(self):
        """Numeracja stron w GO bywa 0- i 1-based — powtórzona strona nie może się dublować."""
        pages = [
            {"items": [transaction(id="a"), transaction(id="b")], "totalCount": 3},
            {"items": [transaction(id="b"), transaction(id="c")], "totalCount": 3},
        ]
        calls = []

        def fake_rpc(method, token, payload, extend=None):
            calls.append(method)
            if method == "users.getMe":
                return {"id": "user-1"}
            return pages[min(len(calls) - 2, len(pages) - 1)]

        with mock.patch.object(cp, "decathlon_rpc", side_effect=fake_rpc):
            items, error = cp.fetch_my_reservations(self.cfg)
        self.assertIsNone(error)
        self.assertEqual([cp._ident(i["id"]) for i in items], ["a", "b", "c"])

    def test_stops_on_empty_page(self):
        def fake_rpc(method, token, payload, extend=None):
            if method == "users.getMe":
                return {"id": "user-1"}
            return {"items": [], "totalCount": 99}

        with mock.patch.object(cp, "decathlon_rpc", side_effect=fake_rpc):
            items, error = cp.fetch_my_reservations(self.cfg)
        self.assertEqual((items, error), ([], None))

    def test_missing_user_id_is_reported(self):
        with mock.patch.object(cp, "decathlon_rpc", return_value={}):
            items, error = cp.fetch_my_reservations(self.cfg)
        self.assertIsNone(items)
        self.assertIn("users.getMe", error)

    def test_http_401_asks_for_login(self):
        error_401 = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b""))
        with mock.patch.object(cp, "decathlon_rpc", side_effect=error_401):
            items, error = cp.fetch_my_reservations(self.cfg)
        self.assertIsNone(items)
        self.assertIn("zaloguj się", error)

    def test_token_problem_short_circuits(self):
        items, error = cp.fetch_my_reservations({"token": "", "browser_mode": True})
        self.assertIsNone(items)
        self.assertIn("brak tokenu", error)


class CancelReservationTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {"token": jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)}

    def test_cancels_with_id(self):
        seen = {}

        def fake_rpc(method, token, payload, extend=None):
            seen.update(method=method, payload=payload)
            return {"processState": "cancelled"}

        with mock.patch.object(cp, "decathlon_rpc", side_effect=fake_rpc):
            ok, message = cp.cancel_reservation("tx-1", self.cfg)
        self.assertTrue(ok)
        self.assertEqual(seen["method"], "transactions.cancel")
        self.assertEqual(seen["payload"], {"id": "tx-1"})
        self.assertEqual(message, "cancelled")

    def test_empty_id_never_calls_api(self):
        with mock.patch.object(cp, "decathlon_rpc", side_effect=AssertionError("nie wolno")):
            ok, message = cp.cancel_reservation("  ", self.cfg)
        self.assertFalse(ok)
        self.assertIn("identyfikatora", message)

    def test_http_error_is_reported(self):
        error_500 = urllib.error.HTTPError("u", 500, "Boom", {}, io.BytesIO(b"szczegoly"))
        with mock.patch.object(cp, "decathlon_rpc", side_effect=error_500):
            ok, message = cp.cancel_reservation("tx-1", self.cfg)
        self.assertFalse(ok)
        self.assertIn("500", message)


class ReservationsIcsTest(unittest.TestCase):
    def reservation(self, **over):
        res = cp.normalize_reservation(transaction())
        res["book_url"] = "https://go.decathlon.pl/l/court-1"
        res.update(over)
        return res

    def test_event_has_start_end_and_place(self):
        ics = cp.reservations_ics([self.reservation()])
        self.assertIn("BEGIN:VEVENT", ics)
        self.assertIn("DTSTART:20260804T180000Z", ics)
        self.assertIn("DTEND:20260804T190000Z", ics)   # duration 60 min
        self.assertIn("UID:tx-1@padel-watch", ics)
        self.assertIn("STATUS:CONFIRMED", ics)
        self.assertIn("BEGIN:VALARM", ics)
        self.assertTrue(ics.endswith("END:VCALENDAR\r\n"))

    def test_crlf_line_endings(self):
        ics = cp.reservations_ics([self.reservation()])
        self.assertNotIn("\n", ics.replace("\r\n", ""))

    def test_cancelled_has_no_alarm(self):
        ics = cp.reservations_ics([self.reservation(cancelled=True)])
        self.assertIn("STATUS:CANCELLED", ics)
        self.assertNotIn("BEGIN:VALARM", ics)

    def test_special_characters_escaped(self):
        ics = cp.reservations_ics([self.reservation(address="Geodezyjna 76, Warszawa")])
        self.assertIn("LOCATION:Geodezyjna 76\\, Warszawa", ics)

    def test_cancelled_carries_a_higher_sequence(self):
        """Bez wyższego SEQUENCE kalendarz zignoruje odwołanie i termin w nim zostanie."""
        czynna = cp.reservations_ics([self.reservation()])
        odwolana = cp.reservations_ics([self.reservation(cancelled=True)])
        self.assertIn("SEQUENCE:0", czynna)
        self.assertIn("SEQUENCE:1", odwolana)
        self.assertIn("STATUS:CANCELLED", odwolana)

    def test_same_uid_so_the_calendar_matches_the_event(self):
        """Odwołanie musi mieć TEN SAM UID co pierwotny wpis — po nim kalendarz łączy je w parę."""
        czynna = cp.reservations_ics([self.reservation()])
        odwolana = cp.reservations_ics([self.reservation(cancelled=True)], method="CANCEL")
        uid = "UID:tx-1@padel-watch"
        self.assertIn(uid, czynna)
        self.assertIn(uid, odwolana)

    def test_cancel_method_produces_a_cancellation_file(self):
        odwolana = cp.reservations_ics([self.reservation(cancelled=True)], method="CANCEL")
        self.assertIn("METHOD:CANCEL", odwolana)
        self.assertNotIn("METHOD:PUBLISH", odwolana)

    def test_publish_stays_the_default(self):
        self.assertIn("METHOD:PUBLISH", cp.reservations_ics([self.reservation()]))

    def test_reservation_without_date_is_skipped(self):
        ics = cp.reservations_ics([self.reservation(start_utc=None)])
        self.assertNotIn("BEGIN:VEVENT", ics)

    def test_long_line_is_folded(self):
        ics = cp.reservations_ics([self.reservation(title="Ł" * 200)])
        for line in ics.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)
        self.assertIn("\r\n ", ics)  # kontynuacja zaczyna się spacją

    def test_short_line_untouched(self):
        self.assertEqual(cp._ics_fold("SUMMARY:Padel"), "SUMMARY:Padel")


class PanelRenewalDipTest(unittest.TestCase):
    """Dołek odnowy: strona odnawia JWT dopiero PO wygaśnięciu, więc 401 bywa przejściowy."""

    def setUp(self):
        self.cfg = {"token": jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600),
                    "browser_mode": True}

    def http_401(self):
        return urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b""))

    def test_retries_with_fresher_token(self):
        calls = []

        def rpc(method, token, payload, extend=None):
            calls.append(token)
            if len(calls) == 1:
                raise self.http_401()
            return {"ok": True}

        with mock.patch.object(cp, "decathlon_rpc", side_effect=rpc), \
                mock.patch.object(cp, "wait_for_fresher_token", return_value="swiezy"):
            doc, token = cp.panel_rpc("users.getMe", self.cfg, "stary", {})
        self.assertEqual(doc, {"ok": True})
        self.assertEqual((calls, token), (["stary", "swiezy"], "swiezy"))
        self.assertEqual(self.cfg["token"], "swiezy")  # kolejne wywołania mają już nowy

    def test_gives_up_when_no_fresher_token_arrives(self):
        with mock.patch.object(cp, "decathlon_rpc", side_effect=self.http_401()), \
                mock.patch.object(cp, "wait_for_fresher_token", return_value=""):
            with self.assertRaises(urllib.error.HTTPError):
                cp.panel_rpc("users.getMe", self.cfg, "stary", {})

    def test_no_retry_without_browser_mode(self):
        """Bez przeglądarki nikt tokenu nie odnowi — czekanie byłoby tylko zwłoką."""
        waited = []
        with mock.patch.object(cp, "decathlon_rpc", side_effect=self.http_401()), \
                mock.patch.object(cp, "wait_for_fresher_token", side_effect=lambda *a, **k: waited.append(1) or ""):
            with self.assertRaises(urllib.error.HTTPError):
                cp.panel_rpc("users.getMe", {"browser_mode": False}, "stary", {})
        self.assertEqual(waited, [])

    def test_other_http_errors_are_not_retried(self):
        error_500 = urllib.error.HTTPError("u", 500, "Boom", {}, io.BytesIO(b""))
        with mock.patch.object(cp, "decathlon_rpc", side_effect=error_500), \
                mock.patch.object(cp, "wait_for_fresher_token", return_value="swiezy"):
            with self.assertRaises(urllib.error.HTTPError):
                cp.panel_rpc("users.getMe", self.cfg, "stary", {})

    def test_reservations_survive_the_dip(self):
        calls = []

        def rpc(method, token, payload, extend=None):
            calls.append((method, token))
            if method == "users.getMe" and len(calls) == 1:
                raise self.http_401()
            if method == "users.getMe":
                return {"id": "user-1"}
            return {"items": [transaction()], "totalCount": 1}

        with mock.patch.object(cp, "decathlon_rpc", side_effect=rpc), \
                mock.patch.object(cp, "wait_for_fresher_token", return_value="swiezy"):
            items, error = cp.fetch_my_reservations(self.cfg)
        self.assertIsNone(error)
        self.assertEqual(len(items), 1)

    def test_cancel_survives_the_dip(self):
        calls = []

        def rpc(method, token, payload, extend=None):
            calls.append(token)
            if len(calls) == 1:
                raise self.http_401()
            return {"processState": "cancelled"}

        with mock.patch.object(cp, "decathlon_rpc", side_effect=rpc), \
                mock.patch.object(cp, "wait_for_fresher_token", return_value="swiezy"):
            ok, message = cp.cancel_reservation("tx-1", self.cfg)
        self.assertTrue(ok)
        self.assertEqual(message, "cancelled")

    def test_wait_returns_first_different_token(self):
        tokens = iter(["stary", "stary", "nowy"])
        # TOKEN_FILE musi być ustawiony: bez pliku funkcja słusznie wraca od razu,
        # a podstawianie token_from_file przy pustym TOKEN_FILE to stan niemożliwy.
        with mock.patch.object(cp, "TOKEN_FILE", "/data/token.json"), \
                mock.patch.object(cp, "token_from_file", side_effect=lambda: next(tokens)), \
                mock.patch.object(cp.time, "sleep"):
            self.assertEqual(cp.wait_for_fresher_token("stary"), "nowy")

    def test_wait_gives_up_after_attempts(self):
        with mock.patch.object(cp, "TOKEN_FILE", "/data/token.json"), \
                mock.patch.object(cp, "token_from_file", return_value="stary"), \
                mock.patch.object(cp.time, "sleep") as slept:
            self.assertEqual(cp.wait_for_fresher_token("stary", attempts=3), "")
        self.assertEqual(slept.call_count, 3)


class LoadConfigNoiseTest(unittest.TestCase):
    def test_missing_config_reported_once_per_process(self):
        """Ta linia leciała przy każdej iteracji monitora i każdym zapytaniu panelu."""
        out = io.StringIO()
        with mock.patch.object(cp, "CONFIG_PATH", "/nie/ma/takiego.json"), \
                mock.patch.object(cp, "_config_warned", False), \
                mock.patch("sys.stdout", out):
            cp.load_config()
            cp.load_config()
        self.assertEqual(out.getvalue().count("używam wartości z ENV"), 1)

    def test_quiet_never_logs(self):
        out = io.StringIO()
        with mock.patch.object(cp, "CONFIG_PATH", "/nie/ma/takiego.json"), \
                mock.patch.object(cp, "_config_warned", False), \
                mock.patch("sys.stdout", out):
            self.assertEqual(cp.load_config(quiet=True), {})
        self.assertEqual(out.getvalue(), "")


class PanelTest(unittest.TestCase):
    """Panel HTTP — sprawdzamy to, co da się sprawdzić bez sieci i przeglądarki."""

    def setUp(self):
        import panel
        self.panel = panel

    def test_static_path_stays_inside_novnc(self):
        with tempfile.TemporaryDirectory() as root:
            page = os.path.join(root, "vnc.html")
            with open(page, "w", encoding="utf-8") as f:
                f.write("x")
            with mock.patch.object(self.panel, "NOVNC_DIR", root):
                # realpath: na macOS /var to dowiązanie do /private/var
                self.assertEqual(self.panel.safe_static_path("/vnc.html"), os.path.realpath(page))
                self.assertIsNone(self.panel.safe_static_path("/../../etc/passwd"))
                self.assertIsNone(self.panel.safe_static_path("/%2e%2e/%2e%2e/etc/passwd"))
                self.assertIsNone(self.panel.safe_static_path("/nie-ma-mnie.js"))

    def test_public_reservation_is_json_serializable(self):
        res = cp.normalize_reservation(transaction())
        res.update(when="wt 04.08 20:00", day="04.08.2026", hours="20:00–21:00", book_url="u")
        payload = self.panel.public_reservation(res)
        self.assertEqual(payload["start"], "2026-08-04T18:00:00+00:00")
        json.dumps(payload)  # nie może wywalić się na datetime

    def test_cache_serves_repeated_reads(self):
        calls = []

        def fake_view(cfg, tz, now=None):
            calls.append(1)
            return [], None

        with mock.patch.object(cp, "reservations_view", side_effect=fake_view), \
                mock.patch.object(cp, "credentials_cfg", return_value={}):
            self.panel._cache.update(at=0, items=None, error=None)
            self.panel.reservations()
            self.panel.reservations()
            self.assertEqual(len(calls), 1)
            self.panel.reservations(force=True)   # po anulowaniu bufor musi ustąpić
            self.assertEqual(len(calls), 2)
        self.panel._cache.update(at=0, items=None, error=None)


if __name__ == "__main__":
    unittest.main()


class DayGridTest(unittest.TestCase):
    """Cały grafik dnia, nie tylko wolne terminy — inaczej nie da się zmierzyć,
    czy ktoś zabrał godzinę, zanim ją w ogóle zobaczyliśmy."""

    NOW = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
    TZ = ZoneInfo("Europe/Warsaw") if ZoneInfo else timezone.utc

    def doc(self):
        item = TestFreeSlots.date_item
        return {"included": [
            item("2026-08-17T13:00:00+00:00", "a"),              # wolny (15:00 lokalnie)
            item("2026-08-17T15:00:00+00:00", "b"),              # wolny (17:00)
            item("2026-08-17T07:00:00+00:00", "c", count=1),     # ZAJĘTY (09:00)
            item("2026-08-17T08:00:00+00:00", "d", count=1),     # ZAJĘTY (10:00)
            item("2026-08-17T09:00:00+00:00", "e", cancelled=True),   # odwołany — poza grafikiem
            item("2026-08-18T13:00:00+00:00", "f"),              # inny dzień
        ]}

    def test_counts_taken_slots_too(self):
        wolne, wszystkie, zajete = cp.day_grid(self.doc(), "L", self.NOW,
                                               date(2026, 8, 17), self.TZ)
        self.assertEqual((wolne, wszystkie), (2, 4))
        # Same liczby nie odpowiadają na pytanie „czy 20:00 było do wzięcia" —
        # dlatego day_grid oddaje też KTÓRE godziny są zajęte.
        self.assertEqual(zajete, ["09:00", "10:00"])

    def test_free_slots_still_hides_taken(self):
        """Regresja: rozdzielenie parsera nie może zmienić tego, co widzi monitor."""
        wolne = cp.free_slots(self.doc(), "L", self.NOW)
        self.assertEqual(len(wolne), 3)   # 2 z 17.08 + 1 z 18.08, zajęte pominięte

    def test_other_days_do_not_leak_in(self):
        _, wszystkie, _ = cp.day_grid(self.doc(), "L", self.NOW, date(2026, 8, 18), self.TZ)
        self.assertEqual(wszystkie, 1)

    def test_log_names_the_missing_hours(self):
        nowe = [s for s in cp.free_slots(self.doc(), "L", self.NOW)
                if s["start_utc"].astimezone(self.TZ).date() == date(2026, 8, 17)]
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            cp.log_day_grids(nowe, {"L": self.doc()}, self.NOW, self.TZ)
        out = buf.getvalue()
        self.assertIn("pon 17.08", out)
        self.assertIn("2 wolne z 4", out)
        # Godziny w nawiasie to sedno: bez nich nie wiadomo, KTÓRA godzina przepadła.
        self.assertIn("2 zajęte (09:00, 10:00), zanim zobaczyliśmy", out)

    def test_full_grid_says_nothing_about_being_late(self):
        """Gdy nic nie jest zajęte, nie sugerujemy, że ktoś nas ubiegł."""
        doc = {"included": [TestFreeSlots.date_item("2026-08-17T13:00:00+00:00", "a")]}
        nowe = cp.free_slots(doc, "L", self.NOW)
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            cp.log_day_grids(nowe, {"L": doc}, self.NOW, self.TZ)
        self.assertIn("1 wolny z 1", buf.getvalue())
        self.assertNotIn("zajętych", buf.getvalue())


class DeferredPushTest(unittest.TestCase):
    """W zrywie push czeka; poza zrywem leci od razu, dokładnie jak dotąd.

    Powód: ntfy.sh to zapytanie do innego serwera, wysyłane w sekundzie publikacji.
    Zmierzone 9. i 10.08: 643–819 ms bez patrzenia na grafik, głównie na tym pushu.
    """

    def setUp(self):
        cp._pending_notifications.clear()
        self.addCleanup(cp._pending_notifications.clear)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for attr, val in (("STATE_PATH", os.path.join(self.dir.name, "s.json")),):
            patcher = mock.patch.object(cp, attr, val)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.env = mock.patch.dict(os.environ, {
            "LISTINGS": "https://go.decathlon.pl/l/" + "a" * 8 + "-1111-2222-3333-444444444444",
            "NTFY_TOPIC": "temat", "FILTERS": "mon-sun:00:00-24:00",
            "AUTO_REGISTER": "false", "TIMEZONE": "Europe/Warsaw",
            "CONFIG_PATH": os.path.join(self.dir.name, "brak.json"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.lid = "aaaaaaaa-1111-2222-3333-444444444444"

    def doc(self, *items):
        return {"data": {"attributes": {"title": "Kort", "price": None,
                                        "datesStats": {"availableListingDates": len(items)}}},
                "included": list(items)}

    def przebieg(self, doc, defer_push=False):
        """Jeden bieg run_once na gotowych danych. Zwraca (log, wywołania ntfy)."""
        buf = io.StringIO()
        with mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: self.lid), \
                mock.patch.object(cp, "ntfy_post", return_value=True) as push, \
                mock.patch("sys.stdout", buf):
            cp.run_once(skip_light=True, prefetched=(self.lid, doc), defer_push=defer_push)
        return buf.getvalue(), push

    def z_terminem(self):
        jutro = datetime.now(timezone.utc) + timedelta(days=7)
        return self.doc(TestFreeSlots.date_item(jutro.isoformat(), "d1"))

    def test_outside_burst_push_is_immediate(self):
        """Zachowanie poza zrywem MUSI zostać nietknięte."""
        self.przebieg(self.doc())                      # baseline
        _, push = self.przebieg(self.z_terminem(), defer_push=False)
        self.assertTrue(push.called)
        self.assertEqual(cp._pending_notifications, [])

    def test_in_burst_push_is_queued_not_sent(self):
        self.przebieg(self.doc())
        out, push = self.przebieg(self.z_terminem(), defer_push=True)
        self.assertFalse(push.called, "push poleciał w zrywie — po to była ta zmiana")
        self.assertEqual(len(cp._pending_notifications), 1)
        self.assertIn("odłożone na po zrywie", out)

    def test_queued_slot_is_saved_so_it_is_not_queued_twice(self):
        """Odłożony termin trafia do stanu — inaczej następna iteracja odłożyłaby go znowu."""
        self.przebieg(self.doc())
        doc = self.z_terminem()
        self.przebieg(doc, defer_push=True)
        self.przebieg(doc, defer_push=True)
        self.assertEqual(len(cp._pending_notifications), 1)

    def test_flush_sends_everything_and_empties_queue(self):
        self.przebieg(self.doc())
        self.przebieg(self.z_terminem(), defer_push=True)
        buf = io.StringIO()
        with mock.patch.object(cp, "ntfy_post", return_value=True) as push, \
                mock.patch("sys.stdout", buf):
            wyslane = cp.flush_notifications()
        self.assertEqual(wyslane, 1)
        self.assertTrue(push.called)
        self.assertEqual(cp._pending_notifications, [])
        self.assertIn("Wysłano 1 odłożone powiadomienie", buf.getvalue())

    def test_flush_retries_then_gives_up_loudly(self):
        """Ciche porzucenie powiadomienia wygląda jak brak terminów — to musi być głośne."""
        self.przebieg(self.doc())
        self.przebieg(self.z_terminem(), defer_push=True)
        buf = io.StringIO()
        # None = wpadka sieciowa (ponawiamy). False to 404 tematu — trwałe, bez ponowień.
        with mock.patch.object(cp, "ntfy_post", return_value=None), \
                mock.patch("sys.stdout", buf):
            for _ in range(cp.NOTIFY_MAX_ATTEMPTS):
                cp.flush_notifications()
        self.assertEqual(cp._pending_notifications, [])
        self.assertIn("Porzucam", buf.getvalue())

    def test_flush_on_empty_queue_is_free(self):
        with mock.patch.object(cp, "ntfy_post") as push:
            self.assertEqual(cp.flush_notifications(), 0)
        push.assert_not_called()

    def test_default_is_immediate(self):
        """Domyślnie zachowanie jak dotąd — odkładanie trzeba włączyć świadomie."""
        self.przebieg(self.doc())
        with mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: self.lid), \
                mock.patch.object(cp, "ntfy_post", return_value=True) as push, \
                mock.patch("sys.stdout", io.StringIO()):
            cp.run_once(skip_light=True, prefetched=(self.lid, self.z_terminem()))
        self.assertTrue(push.called)
        self.assertEqual(cp._pending_notifications, [])


class RemoteHandlerTest(unittest.TestCase):
    """Zdalny strzał z eu-west-1: handler Lambdy + przeniesienie wyniku do dodatku.

    Najważniejsza własność: skoro rejestracja odbyła się w Irlandii, strona lokalna
    NIE MOŻE zarezerwować tego samego terminu drugi raz.
    """

    LID = "aaaaaaaa-1111-2222-3333-444444444444"

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "aws_remote"))
        self.addCleanup(lambda: sys.path.remove(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "aws_remote")))
        import handler
        self.handler = handler
        self.env = mock.patch.dict(os.environ, {"PADEL_SECRET": "tajne"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def doc(self, *godziny):
        start = datetime.now(timezone.utc) + timedelta(days=7)
        included = [TestFreeSlots.date_item(
            start.replace(hour=h, minute=0, second=0, microsecond=0).isoformat(), f"d{h}")
            for h in godziny]
        return {"data": {"attributes": {"title": "Kort", "price": None,
                                        "datesStats": {"availableListingDates": len(included)}}},
                "included": included}

    def zadanie(self, tresc, sekret="tajne"):
        return {"headers": {"x-padel-secret": sekret}, "body": json.dumps(tresc)}

    def tresc(self, **nadpisz):
        baza = {"listing_url": f"https://go.decathlon.pl/l/{self.LID}",
                "filters": "mon-sun:00:00-24:00", "timezone": "Europe/Warsaw",
                "baseline_ids": [], "sprint_seconds": 1, "sprint_threads": 1,
                "salvo": 0, "max_per_run": 5, "name": "Jan", "enabled": True,
                "token": jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)}
        baza.update(nadpisz)
        return baza

    def odpal(self, doc, rejestracja=None):
        """Uruchamia handler z podstawionym pobieraniem i rejestracją."""
        def fake_register(slot, price, cfg, speculative=False):
            cfg["transaction_id"] = "tx-" + slot["date_id"]
            return (rejestracja or (True, "accepted"))
        with mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: self.LID), \
                mock.patch.object(cp, "fetch_listing", return_value=doc), \
                mock.patch.object(cp, "register_slot", side_effect=fake_register), \
                mock.patch("sys.stdout", io.StringIO()):
            return self.handler.lambda_handler(self.zadanie(self.tresc()), None)

    def rozpakuj(self, odp):
        """Dekoduje odpowiedź dokładnie tak, jak zrobi to `call_remote`."""
        self.assertTrue(odp.get("isBase64Encoded"))
        self.assertEqual(odp["headers"]["Content-Encoding"], "gzip")
        return json.loads(gzip.decompress(base64.b64decode(odp["body"])).decode("utf-8"))

    def test_bad_secret_is_rejected_without_details(self):
        """Adres Function URL jest publiczny — odmowa nie może zdradzać, czemu."""
        odp = self.handler.lambda_handler(self.zadanie({}, sekret="zle"), None)
        self.assertEqual(odp["statusCode"], 403)
        self.assertNotIn("tajne", json.dumps(odp))

    def test_missing_secret_on_function_disables_it(self):
        """Funkcja bez ustawionego PADEL_SECRET nie wpuszcza NIKOGO (a nie wszystkich)."""
        with mock.patch.dict(os.environ, {"PADEL_SECRET": ""}):
            odp = self.handler.lambda_handler(self.zadanie({}, sekret=""), None)
        self.assertEqual(odp["statusCode"], 403)

    def test_registers_and_returns_results(self):
        wynik = self.rozpakuj(self.odpal(self.doc(15, 17)))
        self.assertTrue(wynik["ok"])
        self.assertEqual(wynik["listing_id"], self.LID)
        self.assertEqual(len(wynik["registered"]), 2)
        self.assertTrue(all(v[0] for v in wynik["results"].values()))
        self.assertTrue(wynik["timings"]["sprint_ms"] >= 0)

    def test_token_never_appears_in_response(self):
        """Token wraca do domu tylko w postaci, w jakiej przyszedł — czyli wcale."""
        tresc = self.tresc()
        odp = self.odpal(self.doc(15))
        self.assertNotIn(tresc["token"], json.dumps(self.rozpakuj(odp)))
        self.assertNotIn(tresc["token"], json.dumps(odp))

    def test_no_slots_returns_empty_without_doc(self):
        wynik = self.rozpakuj(self.odpal(self.doc()))
        self.assertIsNone(wynik["doc"])
        self.assertEqual(cp.adopt_remote(wynik), (None, None))

    def test_engine_log_travels_home(self):
        """Dziennik z Irlandii musi trafić do Dziennika dodatku — inaczej ślad po
        sekundzie publikacji zostaje tylko w CloudWatch."""
        wynik = self.rozpakuj(self.odpal(self.doc(15)))
        self.assertTrue(any("Sprint" in w for w in wynik["log"]))
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            cp.adopt_remote(wynik)
        self.assertIn("☁", buf.getvalue())

    def test_local_side_does_not_book_again(self):
        """SEDNO: termin zajęty w Irlandii nie może dostać drugiego strzału z domu."""
        wynik = self.rozpakuj(self.odpal(self.doc(15)))
        prefetched, remote = cp.adopt_remote(wynik)
        self.assertIsNotNone(remote)

        katalog = tempfile.mkdtemp()
        with mock.patch.object(cp, "STATE_PATH", os.path.join(katalog, "s.json")), \
                mock.patch.dict(os.environ, {
                    "LISTINGS": f"https://go.decathlon.pl/l/{self.LID}",
                    "NTFY_TOPIC": "", "FILTERS": "mon-sun:00:00-24:00",
                    "AUTO_REGISTER": "true", "AUTO_REGISTER_NAME": "Jan",
                    "AUTO_REGISTER_DRY_RUN": "false", "AUTO_REGISTER_MAX": "5",
                    "CONFIG_PATH": os.path.join(katalog, "brak.json")}), \
                mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: self.LID), \
                mock.patch.object(cp, "register_slot") as lokalny, \
                mock.patch("sys.stdout", io.StringIO()):
            cp.run_once(skip_light=True, prefetched=(self.LID, self.doc()))   # baseline
            cp.run_once(skip_light=True, prefetched=prefetched, remote=remote)
        lokalny.assert_not_called()

    def test_remote_results_reach_the_notification(self):
        """To, co zarezerwowała Irlandia, musi wejść do treści powiadomienia."""
        wynik = self.rozpakuj(self.odpal(self.doc(15)))
        prefetched, remote = cp.adopt_remote(wynik)
        katalog = tempfile.mkdtemp()
        with mock.patch.object(cp, "STATE_PATH", os.path.join(katalog, "s.json")), \
                mock.patch.dict(os.environ, {
                    "LISTINGS": f"https://go.decathlon.pl/l/{self.LID}",
                    "NTFY_TOPIC": "temat", "FILTERS": "mon-sun:00:00-24:00",
                    "AUTO_REGISTER": "true", "AUTO_REGISTER_NAME": "Jan",
                    "CONFIG_PATH": os.path.join(katalog, "brak.json")}), \
                mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: self.LID), \
                mock.patch.object(cp, "ntfy_post", return_value=True) as push, \
                mock.patch("sys.stdout", io.StringIO()):
            cp.run_once(skip_light=True, prefetched=(self.LID, self.doc()))
            cp.run_once(skip_light=True, prefetched=prefetched, remote=remote)
        tresc = " ".join(str(a) for c in push.call_args_list for a in c.args)
        self.assertIn("Auto-rejestracja: OK", tresc)

    def test_token_inside_expiry_margin_still_registers(self):
        """REGRESJA 12.08: zdalna rejestracja padała na tokenie WAŻNYM, ale bliskim końca.

        Przy `browser_mode=False` `ensure_decathlon_token` uznaje token za wygasły już
        TOKEN_EXPIRY_MARGIN (300 s) przed czasem i próbuje serwerowego /auth/refresh,
        który w Decathlon GO zawsze zwraca 401. Token żyje ~15 min, więc wywracało to
        rejestrację przez ostatnią 1/3 jego życia — tak przepadło 17:00.
        """
        zaraz_wygasa = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 120)
        with mock.patch.object(cp, "refresh_decathlon_token") as refresh, \
                mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: self.LID), \
                mock.patch.object(cp, "fetch_listing", return_value=self.doc(15)), \
                mock.patch.object(cp, "decathlon_rpc", return_value={"id": "tx", "processState": "accepted"}), \
                mock.patch("sys.stdout", io.StringIO()):
            odp = self.handler.lambda_handler(
                self.zadanie(self.tresc(token=zaraz_wygasa)), None)
        refresh.assert_not_called()
        wynik = self.rozpakuj(odp)
        self.assertEqual(len(wynik["registered"]), 1,
                         f"rejestracja padła mimo ważnego tokenu: {wynik.get('results')}")

    def test_expired_token_does_not_wait_for_a_browser_that_is_not_there(self):
        """Bez pliku tokenu nie ma na co czekać — czekanie zjadłoby całe okno sprintu."""
        start = time.monotonic()
        with mock.patch.object(cp, "TOKEN_FILE", ""), mock.patch.object(cp, "time") as fake_time:
            fake_time.sleep.side_effect = AssertionError("nie wolno spać bez pliku tokenu")
            fake_time.monotonic = time.monotonic
            self.assertEqual(cp.wait_for_fresher_token("stary"), "")
        self.assertLess(time.monotonic() - start, 1.0)

    def test_memory_allocation_is_always_reported(self):
        """Bez tej liczby nie odróżnisz wolnego serwera od własnego wygłodzenia CPU.

        13.08 strzały z Irlandii zajmowały 185–382 ms, podczas gdy lokalne z Polski
        w tej samej sekundzie 53–81 ms. Nie dało się rozstrzygnąć, bo handler nie
        raportował swojego przydziału pamięci — czyli w Lambdzie przydziału CPU.
        """
        with mock.patch.dict(os.environ, {"AWS_LAMBDA_FUNCTION_MEMORY_SIZE": "512"}):
            wynik = self.rozpakuj(self.odpal(self.doc(15)))
        dziennik = " ".join(wynik["log"])
        self.assertIn("512 MB", dziennik)
        self.assertIn("UŁAMEK rdzenia", dziennik)

    def test_full_core_is_not_flagged(self):
        with mock.patch.dict(os.environ, {"AWS_LAMBDA_FUNCTION_MEMORY_SIZE": "1769"}):
            wynik = self.rozpakuj(self.odpal(self.doc(15)))
        dziennik = " ".join(wynik["log"])
        self.assertIn("pełny rdzeń", dziennik)
        self.assertNotIn("UWAGA", dziennik)

    def test_warm_ping_is_cheap_and_needs_no_listing(self):
        """Rozgrzewka ma odpowiedzieć od razu — jej sens to uniknięcie zimnego startu."""
        odp = self.handler.lambda_handler(self.zadanie({"warm": True}), None)
        wynik = self.rozpakuj(odp)
        self.assertTrue(wynik["warm"])
        self.assertNotIn("doc", wynik)

    def test_warm_ping_still_requires_the_secret(self):
        odp = self.handler.lambda_handler(self.zadanie({"warm": True}, sekret="zle"), None)
        self.assertEqual(odp["statusCode"], 403)


class RemoteClientTest(unittest.TestCase):
    """Klient zdalnego strzału: sekret obowiązkowy, wpadka nie wywraca polowania."""

    def test_response_is_gunzipped(self):
        dane = gzip.compress(json.dumps({"ok": True, "doc": None}).encode())

        class Odp(io.BytesIO):
            headers = {"Content-Encoding": "gzip"}
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with mock.patch.object(cp.urllib.request, "urlopen", return_value=Odp(dane)):
            wynik, blad = cp.call_remote("https://x/y", "s", {"a": 1}, timeout=5)
        self.assertIsNone(blad)
        self.assertTrue(wynik["ok"])

    def test_timeout_is_reported_as_no_answer(self):
        """Rozróżnienie jest istotne: brak odpowiedzi NIE znaczy, że nic nie zapisano."""
        with mock.patch.object(cp.urllib.request, "urlopen",
                               side_effect=cp.urllib.error.URLError("timed out")):
            wynik, blad = cp.call_remote("https://x/y", "s", {}, timeout=1)
        self.assertIsNone(wynik)
        self.assertIn("brak odpowiedzi", blad)

    def test_secret_goes_in_header_and_token_in_body(self):
        zlapane = {}

        class Odp(io.BytesIO):
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, timeout=None):
            zlapane["naglowki"] = dict(req.headers)
            zlapane["tresc"] = json.loads(req.data.decode())
            return Odp(b'{"ok":true}')

        with mock.patch.object(cp.urllib.request, "urlopen", side_effect=fake):
            cp.call_remote("https://x/y", "sekret", {"token": "JWT"}, timeout=5)
        self.assertEqual(zlapane["naglowki"].get("X-padel-secret"), "sekret")
        self.assertEqual(zlapane["tresc"]["token"], "JWT")
        # Token NIE MOŻE trafić do adresu ani nagłówków — tam bywa logowany po drodze.
        self.assertNotIn("JWT", json.dumps(zlapane["naglowki"]))

    def test_payload_carries_raw_filter_string(self):
        """Filtry jadą surowym napisem, żeby obie strony sparsowały je tak samo."""
        with mock.patch.dict(os.environ, {"FILTERS": "mon-fri:15:00-02:00"}):
            tresc = cp.remote_payload("https://go.decathlon.pl/l/x", {"a"}, "Europe/Warsaw",
                                      4.0, 3, {"salvo": 6, "max_per_run": 8, "token": "t"})
        self.assertEqual(tresc["filters"], "mon-fri:15:00-02:00")
        self.assertEqual(tresc["baseline_ids"], ["a"])
        self.assertEqual(tresc["salvo"], 6)


class SalvoStartOffsetTest(SalvoHelpers, unittest.TestCase):
    """Odstęp startu każdego strzału w salwie — rozstrzyga, kto robi „schodek".

    13.08 z Irlandii cztery strzały naraz zajęły 185/217/326/382 ms, a jeden samotny
    121 ms. Bez odstępów startu nie da się orzec, czy kolejkuje serwer, czy nasza pula.
    """

    def test_log_shows_when_each_shot_started(self):
        _, _, out = self.run_with({15: (True, "accepted", "t1"), 17: (True, "accepted", "t2"),
                                   18: (True, "accepted", "t3"), 19: (True, "accepted", "t4")},
                                  cfg=self.cfg(max_per_run=4))
        self.assertIn("start +", out, "brak odstępu startu — pomiar bezużyteczny")

    def test_offsets_are_small_when_shots_really_are_parallel(self):
        """Skoro salwa strzela równolegle, wszystkie starty muszą być blisko zera.

        Gdyby któryś ruszał setki ms później, „schodek" byłby NASZĄ winą, nie serwera.
        """
        bariera = threading.Barrier(4, timeout=5)

        def fake_register(slot, price, cfg, speculative=False):
            bariera.wait()
            return True, "accepted"

        with mock.patch.object(cp, "register_slot", side_effect=fake_register), \
                mock.patch("sys.stdout", io.StringIO()):
            wyniki = cp.fire_salvo(self.slots(15, 17, 18, 19), {},
                                   self.cfg(max_per_run=4), False, 4)
        self.assertEqual(len(wyniki), 4)
        for res in wyniki:
            self.assertIn("start_ms", res)
            self.assertLess(res["start_ms"], 250,
                            f"strzał ruszył {res['start_ms']} ms po salwie — to nie jest równolegle")


class SalvoStaggerTest(SalvoHelpers, unittest.TestCase):
    """Odstęp między strzałami: kolejność w kolejce serwera ma być NASZA.

    14.08 zmierzone: cztery strzały ruszyły z `start +0 ms` co do jednego, a wróciły
    po 21/37/117/282 ms. Skoro startują razem, kolejkuje serwer — a miejsce w kolejce
    było losowe (15:00 wymienione ostatnie weszło w 37 ms, 17:00 trzecie czekało 282 ms).
    """

    def strzel(self, godziny, stagger):
        kolejnosc = []
        lock = threading.Lock()

        def fake_register(slot, price, cfg, speculative=False):
            with lock:
                kolejnosc.append(slot["start_utc"].hour)
            return True, "accepted"

        with mock.patch.object(cp, "register_slot", side_effect=fake_register), \
                mock.patch("sys.stdout", io.StringIO()):
            wyniki = cp.fire_salvo(self.slots(*godziny), {},
                                   self.cfg(stagger=stagger), False, len(godziny))
        return kolejnosc, wyniki

    def test_most_wanted_slot_reaches_the_server_first(self):
        """SEDNO: przy włączonym odstępie termin nr 1 startuje przed pozostałymi."""
        # 25 ms z zapasem na zaplanowanie wątków w wolnym CI — przy pustej atrapie
        # rejestracji to i tak rząd wielkości więcej niż jitter schedulera.
        kolejnosc, _ = self.strzel([20, 19, 18, 17], stagger=25)
        self.assertEqual(kolejnosc[0], 20, f"pierwszy poszedł {kolejnosc[0]}, nie 20:00")
        self.assertEqual(kolejnosc, sorted(kolejnosc, reverse=True))

    def test_offsets_grow_with_position(self):
        _, wyniki = self.strzel([20, 19, 18], stagger=20)
        odstepy = [r["start_ms"] for r in wyniki]
        self.assertLess(odstepy[0], 10)
        self.assertGreater(odstepy[1], 10)
        self.assertGreater(odstepy[2], odstepy[1])

    def test_stagger_is_not_counted_into_shot_duration(self):
        """`ms` ma mierzyć samo żądanie — inaczej odstęp fałszowałby diagnostykę."""
        _, wyniki = self.strzel([20, 19, 18], stagger=40)
        for res in wyniki:
            self.assertLess(res["ms"], 30, "czas strzału zawiera uśpienie odstępu")

    def test_zero_disables_it_completely(self):
        _, wyniki = self.strzel([20, 19, 18], stagger=0)
        for res in wyniki:
            self.assertLess(res["start_ms"], 30)

    def test_bad_value_falls_back_to_default(self):
        _, wyniki = self.strzel([20, 19], stagger="bzdura")
        self.assertEqual(len(wyniki), 2)

    def test_stagger_is_capped(self):
        """Sufit chroni przed literówką w konfiguracji, która zjadłaby okno publikacji."""
        start = time.monotonic()
        self.strzel([20, 19], stagger=100000)
        self.assertLess(time.monotonic() - start, 1.0)


class HuntJournalTest(unittest.TestCase):
    """Dziennik polowań: odkłada się sam, alarmuje oszczędnie.

    Powód powstania: 23.08 publikacja przesunęła się o 40 s i zryw jej nie złapał.
    Zauważyliśmy to tylko dlatego, że użytkownik przysłał log — inaczej wyglądałoby
    to po prostu jak seria gorszych dni.
    """

    TZ = ZoneInfo("Europe/Warsaw") if ZoneInfo else timezone.utc

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        patcher = mock.patch.object(cp, "HUNTS_PATH", os.path.join(self.dir, "hunts.json"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def slot(self, h, dzien=30):
        return {"id": f"L:d{h}", "listing_id": "L", "date_id": f"d{h}",
                "start_utc": datetime(2026, 8, dzien, h - 2, 0, tzinfo=timezone.utc),
                "name": "Rezerwacja godzinna", "count": 0, "limit": 1, "price": None}

    def zapisz(self, godzina="11:00:15", sloty=(20,), wyniki=None, burst="mon-sun:11:00:30",
               shots=(), grid=("niedz 30.08", 7, 12, [], date(2026, 8, 30))):
        # Piąty element to DATA horyzontu. 30.08 wobec 23.08 to +7 dni, czyli publikacja.
        # Bez niej wpis potraktowałby zdarzenie jak odwołanie i nie zapisał godziny.
        teraz = datetime(2026, 8, 23, *[int(x) for x in godzina.split(":")], tzinfo=self.TZ)
        sl = [self.slot(h) for h in sloty]
        wyn = wyniki if wyniki is not None else {s["id"]: (True, "accepted") for s in sl}
        env = {"BURST": burst, "BURST_SECONDS": "60"}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch("sys.stdout", buf), \
                mock.patch.object(cp, "ntfy_post", return_value=True) as push:
            wpis = cp.record_hunt(teraz, self.TZ, sl, wyn, list(shots), grid, False, "temat")
        return wpis, push, buf.getvalue()

    def test_entry_is_written_and_readable(self):
        wpis, _, _ = self.zapisz()
        self.assertEqual(wpis["date"], "2026-08-23")
        self.assertEqual(wpis["free"], 7)
        self.assertEqual(cp.load_hunts()[0]["registered"], ["niedz 30.08 20:00"])

    def test_one_entry_per_day_even_with_batched_publication(self):
        """Publikacja przychodzi partiami — to NADAL jeden dzień, nie trzy."""
        self.zapisz(sloty=(20,))
        self.zapisz(godzina="11:00:16", sloty=(19,))
        self.zapisz(godzina="11:00:17", sloty=(18,))
        wpisy = cp.load_hunts()
        self.assertEqual(len(wpisy), 1)
        self.assertEqual(len(wpisy[0]["registered"]), 3)

    def test_publication_outside_burst_raises_alarm(self):
        """SEDNO: 23.08 publikacja o 11:00:15, zryw od 11:00:30 — to musi krzyknąć."""
        wpis, push, out = self.zapisz(godzina="11:00:15", burst="mon-sun:11:00:30")
        self.assertFalse(wpis["aligned"])
        self.assertTrue(push.called)
        self.assertIn("POZA zrywem", out)

    def test_publication_inside_burst_is_quiet(self):
        wpis, push, _ = self.zapisz(godzina="11:00:40", burst="mon-sun:11:00:30")
        self.assertTrue(wpis["aligned"])
        push.assert_not_called()

    def test_alert_fires_once_per_day_not_per_batch(self):
        """Push przychodzący przy każdej partii przestałby być czytany."""
        _, push1, _ = self.zapisz(godzina="11:00:15", sloty=(20,))
        _, push2, _ = self.zapisz(godzina="11:00:16", sloty=(19,))
        self.assertTrue(push1.called)
        push2.assert_not_called()

    def test_total_failure_raises_alarm_too(self):
        wyniki = {"L:d20": (False, 'HTTP 409: {"message":"No available seats"}')}
        wpis, push, _ = self.zapisz(godzina="11:00:40", sloty=(20,), wyniki=wyniki)
        self.assertEqual(wpis["failed"], [{"when": "niedz 30.08 20:00", "why": "zajęty (409)"}])
        self.assertTrue(push.called)

    def test_shots_are_kept_for_later_analysis(self):
        strzaly = [{"when": "20:00", "ok": True, "ms": 387, "start_ms": 0, "salwa": True}]
        wpis, _, _ = self.zapisz(shots=strzaly)
        self.assertEqual(wpis["shots"][0]["ms"], 387)

    def test_history_is_capped(self):
        cp.save_hunts([{"date": f"2026-06-{d:02d}"} for d in range(1, 29)] * 4)
        self.assertLessEqual(len(cp.load_hunts()), cp.HUNTS_KEEP)

    def test_corrupt_file_does_not_break_the_hunt(self):
        with open(cp.HUNTS_PATH, "w", encoding="utf-8") as f:
            f.write("{to nie jest json")
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(cp.load_hunts(), [])
        wpis, _, _ = self.zapisz()
        self.assertEqual(wpis["date"], "2026-08-23")

    def test_no_burst_configured_means_no_false_alarm(self):
        wpis, push, _ = self.zapisz(burst="")
        self.assertIsNone(wpis["aligned"])
        push.assert_not_called()


class HuntJournalEndToEndTest(unittest.TestCase):
    """Czy prawdziwy przebieg odkłada wpis — bez tego cała zakładka jest pusta."""

    LID = "aaaaaaaa-1111-2222-3333-444444444444"

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for attr, val in (("STATE_PATH", "s.json"), ("HUNTS_PATH", "hunts.json")):
            patcher = mock.patch.object(cp, attr, os.path.join(self.dir, val))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.env = mock.patch.dict(os.environ, {
            "LISTINGS": f"https://go.decathlon.pl/l/{self.LID}",
            "NTFY_TOPIC": "", "FILTERS": "mon-sun:00:00-24:00", "TIMEZONE": "Europe/Warsaw",
            "AUTO_REGISTER": "true", "AUTO_REGISTER_NAME": "Jan",
            "AUTO_REGISTER_DRY_RUN": "false", "AUTO_REGISTER_MAX": "5",
            "BURST": "mon-sun:11:00:30", "BURST_SECONDS": "60",
            "CONFIG_PATH": os.path.join(self.dir, "brak.json")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def doc(self, *godziny):
        start = datetime.now(timezone.utc) + timedelta(days=7)
        return {"data": {"attributes": {"title": "Kort", "price": None,
                                        "datesStats": {"availableListingDates": len(godziny)}}},
                "included": [TestFreeSlots.date_item(
                    start.replace(hour=h, minute=0, second=0, microsecond=0).isoformat(), f"d{h}")
                    for h in godziny]}

    def bieg(self, doc):
        with mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: self.LID), \
                mock.patch.object(cp, "register_slot", return_value=(True, "accepted")), \
                mock.patch("sys.stdout", io.StringIO()):
            cp.run_once(skip_light=True, prefetched=(self.LID, doc))

    def test_run_writes_a_hunt_entry_with_shot_times(self):
        self.bieg(self.doc())                    # baseline
        self.bieg(self.doc(15, 17))
        wpisy = cp.load_hunts()
        self.assertEqual(len(wpisy), 1)
        self.assertEqual(len(wpisy[0]["registered"]), 2)
        self.assertTrue(wpisy[0]["shots"], "brak czasów strzałów — dziennik traci sens")
        self.assertIn("total", wpisy[0])

    def test_panel_serves_the_journal(self):
        """Zakładka „Polowania" czyta TYLKO plik — nie może zaszkodzić polowaniu."""
        self.bieg(self.doc())
        self.bieg(self.doc(15))
        import importlib, sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "padel_browser"))
        try:
            panel = importlib.import_module("panel")
        finally:
            _sys.path.pop(0)
        with mock.patch.object(panel.check_padel, "HUNTS_PATH", cp.HUNTS_PATH):
            wpisy = panel.check_padel.load_hunts()
        self.assertEqual(len(wpisy), 1)
        self.assertEqual(len(wpisy[0]["registered"]), 1)
        # Slot budujemy w UTC, a etykieta jest LOKALNA — wyliczamy ją tak samo jak kod,
        # inaczej test przestałby przechodzić przy zmianie czasu.
        oczekiwana = (datetime.now(timezone.utc) + timedelta(days=7)).replace(
            hour=15, minute=0, second=0, microsecond=0).astimezone(cp._log_tz())
        self.assertEqual(wpisy[0]["registered"][0], cp.fmt_when(oczekiwana, short=True))


class NeverSeenTest(HuntJournalTest):
    """Rozróżnienie, po które powstała ta zmiana: przegrany WYŚCIG vs godzina,
    której nigdy nie zobaczyliśmy jako wolnej.

    Pytanie użytkownika brzmiało „jakim cudem ktoś zawsze zajmuje 20:00". Bez tego
    rozróżnienia nie da się odpowiedzieć: 409 znaczy, że widzieliśmy termin wolny
    i przegraliśmy zapis (to jest do optymalizowania), a brak w ogóle znaczy, że
    zniknął przed naszym pierwszym spojrzeniem (tego nie wygra żadna prędkość).
    """

    def test_lost_race_is_not_reported_as_never_seen(self):
        """20:00 z 409 to przegrany wyścig — widzieliśmy je wolne."""
        wyniki = {"L:d20": (False, 'HTTP 409: {"message":"No available seats"}')}
        wpis, _, _ = self.zapisz(godzina="11:00:40", sloty=(20,), wyniki=wyniki,
                                 grid=("niedz 30.08", 5, 6, ["20:00"]))
        self.assertEqual([f["when"] for f in wpis["failed"]], ["niedz 30.08 20:00"])
        self.assertEqual(wpis["never_seen"], [],
                         "przegrany wyścig NIE jest 'nigdy nie widziane'")

    def test_hour_taken_before_we_looked_is_flagged(self):
        """SEDNO: 20:00 zajęte, a my nawet w nie nie strzelaliśmy."""
        wpis, _, _ = self.zapisz(godzina="11:00:40", sloty=(15,),
                                 grid=("niedz 30.08", 5, 7, ["20:00", "19:00"]))
        self.assertEqual(wpis["never_seen"], ["19:00", "20:00"])

    def test_our_own_bookings_are_not_flagged(self):
        """Zarezerwowany przez nas termin też jest 'zajęty' w kolejnej migawce —
        gdyby go nie odjąć, dziennik oskarżałby nas o kradzież własnych terminów."""
        wpis, _, _ = self.zapisz(godzina="11:00:40", sloty=(20,),
                                 grid=("niedz 30.08", 5, 6, ["20:00"]))
        self.assertEqual(wpis["registered"], ["niedz 30.08 20:00"])
        self.assertEqual(wpis["never_seen"], [])

    def test_taken_accumulates_across_batches(self):
        """Publikacja przychodzi partiami — każda migawka pokazuje inny fragment."""
        self.zapisz(godzina="11:00:40", sloty=(15,), grid=("niedz 30.08", 5, 7, ["20:00"]))
        wpis, _, _ = self.zapisz(godzina="11:00:41", sloty=(17,),
                                 grid=("niedz 30.08", 4, 7, ["19:00"]))
        self.assertEqual(wpis["taken"], ["19:00", "20:00"])
        self.assertEqual(wpis["never_seen"], ["19:00", "20:00"])


class HorizonDayTest(unittest.TestCase):
    """REGRESJA 26.08: raport opisał zły dzień i zaliczył NASZ termin jako stracony.

    Tego dnia nowe terminy przyszły z DWÓCH dni naraz: publikacja dotyczyła 02.09,
    a odwołania 01.09. Raport wziął dzień najwcześniejszy, czyli odwołania, i wypisał
    „nigdy nie pokazane jako wolne: … 18:00", choć 18:00 na 01.09 mieliśmy od 25.08.
    """

    TZ = ZoneInfo("Europe/Warsaw") if ZoneInfo else timezone.utc
    NOW = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)

    def doc(self):
        item = TestFreeSlots.date_item
        return {"included": [
            # 01.09 — dzień z odwołań: 15:00 wolne, 18:00 NASZE, 19:00 cudze
            item("2026-09-01T13:00:00+00:00", "a"),
            item("2026-09-01T16:00:00+00:00", "nasze18", count=1),
            item("2026-09-01T17:00:00+00:00", "obce19", count=1),
            # 02.09 — HORYZONT, świeża publikacja: 15:00 wolne, 20:00 cudze
            item("2026-09-02T13:00:00+00:00", "c"),
            item("2026-09-02T18:00:00+00:00", "obce20", count=1),
        ]}

    def nowe(self):
        return [{"id": "L:a", "listing_id": "L",
                 "start_utc": datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc)},
                {"id": "L:c", "listing_id": "L",
                 "start_utc": datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc)}]

    def uruchom(self, held=()):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            wynik = cp.log_day_grids(self.nowe(), {"L": self.doc()}, self.NOW, self.TZ, held)
        return wynik, buf.getvalue()

    def test_reports_the_horizon_day_not_the_earliest(self):
        wynik, _ = self.uruchom()
        self.assertEqual(wynik[0], "śr 02.09",
                         f"raport opisał {wynik[0]} zamiast dnia publikacji")

    def test_our_own_booking_is_not_counted_as_lost(self):
        wynik, out = self.uruchom(held={"L:nasze18"})
        self.assertNotIn("18:00", out.split("01.09")[1].split("\n")[0],
                         "nasz własny termin zgłoszony jako stracony")

    def test_earlier_days_are_not_described_as_a_race(self):
        """„Zanim zobaczyliśmy grafik" ma sens TYLKO dla dnia publikacji."""
        _, out = self.uruchom(held={"L:nasze18"})
        linia_01 = [w for w in out.splitlines() if "01.09" in w][0]
        linia_02 = [w for w in out.splitlines() if "02.09" in w][0]
        self.assertIn("przez innych", linia_01)
        self.assertIn("zanim zobaczyliśmy grafik", linia_02)


class PreflightTokenTest(unittest.TestCase):
    """Kontrola sesji PRZED polowaniem.

    27.08 wszystkie pięć strzałów padło w 0 ms z powodem „token" — sesja nie żyła,
    a dowiedzieliśmy się o tym PO publikacji, tracąc cztery wolne terminy (15, 17,
    19, 20). Push ma przychodzić na tyle wcześnie, żeby dało się zalogować.
    """

    TZ = ZoneInfo("Europe/Warsaw") if ZoneInfo else timezone.utc

    def setUp(self):
        cp._preflight_done_on = None
        self.addCleanup(setattr, cp, "_preflight_done_on", None)
        self.dir = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {
            "BURST": "mon-sun:11:00:05", "BURST_SECONDS": "60",
            "TOKEN_CHECK_BEFORE": "30", "AUTO_REGISTER": "true",
            "CONFIG_PATH": os.path.join(self.dir, "brak.json")})
        self.env.start()
        self.addCleanup(self.env.stop)
        patcher = mock.patch.object(cp, "STATE_PATH", os.path.join(self.dir, "s.json"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def zywy(self):
        return jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)

    def sprawdz(self, o_ktorej="10:30:30", token=None, rpc=None):
        # 10:30:30 leży w oknie kontroli (zryw 11:00:05 minus 30 min = 10:30:05,
        # okno trwa 90 s). „10:30:00" byłoby PIĘĆ SEKUND za wcześnie.
        chwila = datetime(2026, 8, 28, *[int(x) for x in o_ktorej.split(":")], tzinfo=self.TZ)
        buf = io.StringIO()
        cfg = {"token": token if token is not None else self.zywy()}
        with mock.patch.object(cp, "build_reg_cfg", return_value=cfg), \
                mock.patch.object(cp, "decathlon_rpc",
                                  side_effect=rpc or (lambda *a, **k: {})) as wywolanie, \
                mock.patch.object(cp, "ntfy_post", return_value=True) as push, \
                mock.patch("sys.stdout", buf):
            wynik = cp.preflight_token(chwila, self.TZ, "temat", "https://kort")
        return wynik, push, buf.getvalue(), wywolanie

    def test_fires_exactly_thirty_minutes_before_the_burst(self):
        wynik, _, out, _ = self.sprawdz("10:30:05")
        self.assertTrue(wynik)
        self.assertIn("Sesja Decathlon sprawdzona", out)

    def test_silent_at_any_other_time(self):
        for kiedy in ("09:00:00", "10:25:00", "10:45:00", "11:00:05"):
            self.assertIsNone(self.sprawdz(kiedy)[0], f"odpaliło się o {kiedy}")

    def test_runs_once_per_day(self):
        self.assertTrue(self.sprawdz("10:30:05")[0])
        self.assertIsNone(self.sprawdz("10:30:40")[0], "druga kontrola tego samego dnia")

    def test_missing_token_raises_urgent_push(self):
        """To jest dokładnie przypadek z 27.08."""
        wynik, push, out, _ = self.sprawdz(token="")
        self.assertFalse(wynik)
        self.assertTrue(push.called)
        self.assertIn("SESJA NIE ŻYJE", out)
        self.assertEqual(push.call_args.kwargs.get("priority"), "urgent")

    def test_server_rejecting_a_valid_looking_token_also_alarms(self):
        """Token z ważnym `exp` może być już unieważniony po stronie serwera."""
        def odrzuca(*a, **k):
            raise cp.urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        wynik, push, _, _ = self.sprawdz(rpc=odrzuca)
        self.assertFalse(wynik)
        self.assertTrue(push.called)

    def test_network_failure_is_not_reported_as_dead_session(self):
        """Fałszywy alarm o martwej sesji byłby gorszy niż jego brak."""
        def pada(*a, **k):
            raise cp.urllib.error.URLError("sieć padła")
        wynik, push, _, _ = self.sprawdz(rpc=pada)
        self.assertTrue(wynik)
        push.assert_not_called()

    def test_disabled_by_zero(self):
        with mock.patch.dict(os.environ, {"TOKEN_CHECK_BEFORE": "0"}):
            self.assertIsNone(self.sprawdz("10:30:05")[0])

    def test_no_check_without_auto_register(self):
        with mock.patch.dict(os.environ, {"AUTO_REGISTER": "false"}):
            self.assertIsNone(self.sprawdz("10:30:05")[0])

    def test_day_matching_survives_a_foreign_locale(self):
        """Dni zrywu muszą pasować niezależnie od ustawień językowych kontenera."""
        with mock.patch.dict(os.environ, {"BURST": "fri:11:00:05"}):
            piatek = datetime(2026, 8, 28, 10, 30, 5, tzinfo=self.TZ)   # 28.08.2026 to piątek
            self.assertIsNotNone(cp.burst_start_today(piatek, self.TZ))
        with mock.patch.dict(os.environ, {"BURST": "mon:11:00:05"}):
            self.assertIsNone(cp.burst_start_today(piatek, self.TZ))


class PublicationWindowTest(HuntJournalTest):
    """REGRESJA 28.08: „nigdy nie pokazane jako wolne" liczyło CAŁĄ dobę.

    Wpis mówił `8 wolnych z 11` i jednocześnie wymieniał 8 godzin jako zniknięte
    przed naszym pierwszym spojrzeniem — liczby, które nie mogą być naraz prawdziwe.
    Przyczyna: zajęte godziny sumowały się przez wszystkie wykrycia w ciągu dnia,
    więc zwykły ruch z popołudnia lądował w diagnostyce publikacji.
    """

    def zapisz_z_zajetymi(self, godzina, zajete):
        return self.zapisz(godzina=godzina, sloty=(20,),
                           grid=("pt 04.09", 8, 11, list(zajete), date(2026, 8, 30)))

    def test_batches_within_the_publication_moment_still_add_up(self):
        """Publikacja sypie partiami — kolejne migawki w tej samej chwili muszą się sumować."""
        self.zapisz_z_zajetymi("11:00:15", ["09:00"])
        wpis, _, _ = self.zapisz_z_zajetymi("11:00:16", ["10:00"])
        self.assertEqual(wpis["taken"], ["09:00", "10:00"])

    def test_bookings_hours_later_are_not_counted_as_lost(self):
        """SEDNO: ktoś rezerwuje o 14:00 — to zwykły ruch, nie nasza porażka."""
        self.zapisz_z_zajetymi("11:00:15", ["09:00"])
        wpis, _, _ = self.zapisz_z_zajetymi("14:30:00", ["10:00", "11:00", "12:00"])
        self.assertEqual(wpis["taken"], ["09:00"],
                         "godziny zajęte godziny później trafiły do diagnostyki publikacji")
        self.assertNotIn("12:00", wpis["never_seen"])

    def test_later_snapshot_still_refreshes_the_grid_numbers(self):
        """Późniejsze migawki mają aktualizować licznik wolnych — to nadal użyteczne."""
        self.zapisz_z_zajetymi("11:00:15", ["09:00"])
        wpis, _, _ = self.zapisz(godzina="15:00:00", sloty=(19,),
                                 grid=("pt 04.09", 2, 11, ["09:00", "10:00"], date(2026, 8, 30)))
        self.assertEqual(wpis["free"], 2)


class CancellationIsNotPublicationTest(HuntJournalTest):
    """REGRESJA 28.08: odwołanie na DZIŚ zostało ogłoszone jako publikacja.

    O 08:57 ktoś zwolnił termin na ten sam dzień. Dziennik wziął pierwsze nowe
    terminy doby za publikację, zapisał „publikacja 08:57:20" i wysłał fałszywy
    alarm „poza zrywem". Prawdziwa publikacja przyszła o 11:00:36 — czyli tak samo
    jak dzień wcześniej. Na tej podstawie doradzałem przebudowę okien, której
    nie było potrzeba.
    """

    def odwolanie(self, godzina):
        """Nowy wolny termin na DZIŚ — tak wygląda odwołanie."""
        return self.zapisz(godzina=godzina,
                           grid=("pt 28.08", 1, 11, ["09:00"], date(2026, 8, 23)))

    def publikacja(self, godzina):
        return self.zapisz(godzina=godzina,
                           grid=("pt 04.09", 8, 11, ["17:00"], date(2026, 8, 30)))

    def test_cancellation_is_not_recorded_as_publication(self):
        wpis, push, out = self.odwolanie("08:57:20")
        self.assertIsNone(wpis["first_seen"], "odwołanie zapisane jako godzina publikacji")
        self.assertNotIn("POZA zrywem", out)
        push.assert_not_called()

    def test_cancellation_still_records_what_we_won(self):
        """Odwołanie ma trafić do dziennika — tylko nie jako publikacja."""
        wpis, _, _ = self.odwolanie("08:57:20")
        self.assertEqual(len(wpis["registered"]), 1)

    def test_real_publication_later_the_same_day_is_recorded(self):
        self.odwolanie("08:57:20")
        wpis, _, _ = self.publikacja("11:00:36")
        self.assertEqual(wpis["first_seen"], "11:00:36")
        self.assertTrue(wpis["aligned"], "publikacja o 11:00:36 mieści się w zrywie 11:00:30+60s")

    def test_alarm_fires_for_a_real_publication_outside_the_burst(self):
        """Prawdziwy rozjazd nadal ma krzyczeć — nie stępiamy alarmu."""
        wpis, push, out = self.publikacja("08:57:20")
        self.assertEqual(wpis["first_seen"], "08:57:20")
        self.assertFalse(wpis["aligned"])
        self.assertTrue(push.called)
        self.assertIn("POZA zrywem", out)


class LogLevelTest(unittest.TestCase):
    """Dzień pracy dodatku to ~2000 linii, z czego prawie wszystko to rutynowe pytanie
    „czy coś się zwolniło?". Poziomy mają wyciszyć to pytanie, nie odpowiedzi."""

    def zloguj(self, *args, level=None, prog="info"):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"LOG_LEVEL": prog}), \
             contextlib.redirect_stdout(buf):
            if level is None:
                cp.log(*args)
            else:
                cp.log(*args, level=level)
        return buf.getvalue()

    def test_default_threshold_hides_debug_and_shows_info(self):
        self.assertEqual("", self.zloguj("Brak nowych wolnych terminów.", level="debug"))
        self.assertIn("Grafik", self.zloguj("📋 Grafik na pt 04.09"))

    def test_debug_threshold_shows_everything(self):
        self.assertIn("Brak", self.zloguj("Brak nowych", level="debug", prog="debug"))

    def test_bang_and_warning_glyphs_are_warnings(self):
        """Konwencja znaków istnieje w kodzie od początku — czytamy ją, zamiast
        dopisywać level= w czterdziestu miejscach."""
        self.assertIn("!", self.zloguj("! Nie wysłano 2 powiadomień", prog="warn"))
        self.assertIn("⚠", self.zloguj("⚠ JWT wygasł", prog="warn"))
        self.assertEqual("", self.zloguj("📋 Grafik na pt 04.09", prog="warn"))

    def test_failed_shot_is_an_error(self):
        self.assertIn("✗", self.zloguj("✗ Token martwy", prog="error"))
        self.assertEqual("", self.zloguj("! Ponowię", prog="error"))

    def test_broken_level_name_does_not_silence_the_addon(self):
        """SEDNO: literówka w konfiguracji nie może oślepić dodatku w dniu publikacji."""
        self.assertIn("Grafik", self.zloguj("📋 Grafik", prog="infoo"))
        self.assertIn("Grafik", self.zloguj("📋 Grafik", prog=""))

    def test_explicit_level_wins_over_the_glyph(self):
        self.assertEqual("", self.zloguj("= Kort: 11 dostępnych", level="debug"))

    def test_leading_whitespace_does_not_hide_the_glyph(self):
        self.assertIn("✗", self.zloguj("   ✗ strzał odrzucony", prog="error"))


class LeadShotTest(SalvoHelpers, unittest.TestCase):
    """STRZAŁ CZOŁOWY — test hipotezy o kolejce po stronie serwera.

    25.08 cztery strzały ruszyły razem (start +0/+0/+8/+16 ms), a wróciły po
    84 / 700 / 800 / 725 ms. Równoległe żądania nie różnią się dziesięciokrotnie,
    jeśli nic ich nie blokuje — więc serwer najpewniej obsługuje zapisy do tego kortu
    po kolei. Jeśli tak, to spychaliśmy 20:00 na koniec WŁASNEJ kolejki, strzelając
    w nią razem z pięcioma innymi terminami.

    Poprawka: najpożądańszy termin idzie sam i pierwszy. Te testy pilnują, że naprawdę
    idzie sam — bo inaczej eksperyment nie mierzy tego, co miał mierzyć.
    """

    def czasy(self, outcomes, cfg=None, slots=None):
        """Zwraca {godzina: (start, koniec)} — pozwala udowodnić, KTO na kogo czekał."""
        zapis = {}

        def fake_register(slot, price, local_cfg, speculative=False):
            h = slot["start_utc"].hour
            start = time.monotonic()
            time.sleep(0.05)          # żeby nakładanie się strzałów było widoczne
            zapis[h] = (start, time.monotonic())
            ok, msg, tx = outcomes[h]
            if ok:
                local_cfg["transaction_id"] = tx
            return ok, msg

        with mock.patch.object(cp, "register_slot", side_effect=fake_register), \
                mock.patch.object(cp, "cancel_reservation", return_value=(True, "ok")), \
                mock.patch("sys.stdout", io.StringIO()):
            cp.auto_register_new_slots(slots or self.slots(15, 17, 18, 19), {},
                                       cfg or self.cfg(lead=True), set())
        return zapis

    PRZEGRANE = {15: (False, "409", ""), 17: (False, "409", ""),
                 18: (False, "409", ""), 19: (False, "409", "")}

    def test_lead_finishes_before_the_rest_even_start(self):
        """SEDNO: 19:00 (najpożądańszy przy order=latest) nie dzieli kolejki z nikim."""
        z = self.czasy(self.PRZEGRANE)
        koniec_czolowego = z[19][1]
        for h in (18, 17, 15):
            self.assertGreaterEqual(
                z[h][0], koniec_czolowego,
                f"{h}:00 ruszył, zanim czołowy 19:00 skończył — strzał nie był samotny")

    def test_the_rest_still_fire_in_parallel_with_each_other(self):
        """Czołowy ma być sam, ale reszta nie może iść gęsiego — to kosztowałoby sekundy."""
        z = self.czasy(self.PRZEGRANE)
        starty = sorted(z[h][0] for h in (18, 17, 15))
        self.assertLess(starty[-1] - starty[0], 0.04,
                        "reszta salwy poszła po kolei zamiast równolegle")

    def test_disabled_lead_keeps_everything_parallel(self):
        """Wyłącznik musi realnie wracać do starego zachowania — inaczej nie ma odwrotu."""
        z = self.czasy(self.PRZEGRANE, cfg=self.cfg(lead=False))
        starty = sorted(z[h][0] for h in (19, 18, 17, 15))
        self.assertLess(starty[-1] - starty[0], 0.04)

    def test_single_target_is_not_split(self):
        z = self.czasy({19: (False, "409", "")}, slots=self.slots(19))
        self.assertEqual(set(z), {19})

    def test_preference_and_limit_survive_the_split(self):
        """Podział na dwie fale nie może zmienić TEGO, co zostaje zarezerwowane."""
        wygrane_wszedzie = {h: (True, "OK", f"tx{h}") for h in (15, 17, 18, 19)}
        self.cancelled = []

        def fake_cancel(tx, _cfg):
            self.cancelled.append(tx)
            return True, "cancelled"

        def fake_register(slot, price, local_cfg, speculative=False):
            ok, msg, tx = wygrane_wszedzie[slot["start_utc"].hour]
            local_cfg["transaction_id"] = tx
            return ok, msg

        with mock.patch.object(cp, "register_slot", side_effect=fake_register), \
                mock.patch.object(cp, "cancel_reservation", side_effect=fake_cancel), \
                mock.patch("sys.stdout", io.StringIO()):
            _res, reg = cp.auto_register_new_slots(self.slots(15, 17, 18, 19), {},
                                                   self.cfg(lead=True), set())
        # limit=1, order=latest -> zostaje 19:00, reszta oddana z powrotem
        self.assertIn("s19", reg)
        self.assertEqual(sorted(self.cancelled), ["tx15", "tx17", "tx18"])


class DataAgeTest(SalvoHelpers, unittest.TestCase):
    """WIEK DANYCH — liczba, której do tej pory nie mierzyliśmy.

    24.08 strzał w 20:00 trwał 74 ms i wrócił 409. Sam zapis był więc na podłodze
    tego, co osiągalne — a miejsce i tak zniknęło, ZANIM zapytaliśmy. Bez wieku danych
    nie da się odróżnić „byliśmy za wolni" od „patrzyliśmy na nieaktualny grafik",
    a to dwa różne problemy z dwoma różnymi poprawkami.
    """

    def strzel(self, seen_at):
        with mock.patch.object(cp, "register_slot", return_value=(False, "409")), \
                mock.patch("sys.stdout", io.StringIO()):
            cfg = self.cfg(seen_at=seen_at, salvo=4)
            cp.auto_register_new_slots(self.slots(18, 19), {}, cfg, set())
        return cfg["shots"]

    def test_age_is_measured_from_the_moment_we_saw_the_grid(self):
        strzaly = self.strzel(time.monotonic() - 0.4)
        self.assertTrue(strzaly)
        for s in strzaly:
            self.assertGreaterEqual(s["seen_ms"], 400)
            self.assertLess(s["seen_ms"], 2000)

    def test_missing_reference_point_reports_nothing_not_zero(self):
        """Brak punktu odniesienia ma dawać None. Zero kłamałoby, że dane były świeże."""
        for s in self.strzel(None):
            self.assertIsNone(s["seen_ms"])

    def test_shot_description_shows_the_age(self):
        opis = cp.fmt_shot({"ms": 74, "start_ms": 0, "seen_ms": 640})
        self.assertIn("74 ms", opis)
        self.assertIn("sprzed 640 ms", opis)

    def test_shot_description_stays_readable_without_the_age(self):
        self.assertEqual(cp.fmt_shot({"ms": 74, "start_ms": 0}), "start +0 ms, 74 ms")


class LeadShotDisabledByDefaultTest(SalvoHelpers, unittest.TestCase):
    """31.08 obaliło hipotezę o kolejce po naszej stronie — strzał czołowy ma być WYŁĄCZONY.

    Czołowy strzał w 19:00 poszedł sam, na danych sprzed 1 ms, i trwał 730 ms. Skoro
    samotność nie skróciła zapisu, kolejka nie jest nasza. A drugi strzał zapłacił za
    czekanie: 17:00 ruszyło na danych sprzed 732 ms zamiast ~1 ms.
    """

    def test_default_is_off(self):
        self.assertFalse(cp.boolish(cp.build_reg_cfg({}, None)["lead"]))

    def test_default_config_really_fires_everything_at_once(self):
        """Sam domyślny wpis to za mało — liczy się, czy strzały naprawdę lecą razem."""
        starty = {}

        def fake_register(slot, price, local_cfg, speculative=False):
            starty[slot["start_utc"].hour] = time.monotonic()
            time.sleep(0.05)
            return False, "409"

        cfg = self.cfg()
        cfg.pop("lead", None)          # dokładnie to, co daje domyślna konfiguracja
        with mock.patch.object(cp, "register_slot", side_effect=fake_register), \
                mock.patch("sys.stdout", io.StringIO()):
            cp.auto_register_new_slots(self.slots(15, 17, 18, 19), {}, cfg, set())
        rozrzut = max(starty.values()) - min(starty.values())
        self.assertLess(rozrzut, 0.04, "strzały rozjechały się w czasie — czołowy wciąż działa")

    def test_the_switch_still_works_for_a_repeat_experiment(self):
        """Wyłącznik zostaje: gdyby kiedyś trzeba było powtórzyć pomiar."""
        self.assertTrue(cp.boolish(cp.build_reg_cfg({"auto_register_lead": True}, None)["lead"]))


class HedgedShotTest(SalvoHelpers, unittest.TestCase):
    """STRZAŁ REDUNDANTNY: kilka równoległych zapisów w najcenniejszy termin.

    Czas przetwarzania zapisu przez serwer to loteria — 61, 62, 63, 71, 115, 150, 157,
    178, 188, 236, 251, 730 ms — bez związku z czymkolwiek, co robimy. Jedno losowanie
    zamieniamy więc na minimum z kilku. Ten sam termin trafiony dwukrotnie dawał już
    62 i 251 ms (30.08) oraz 700 i 68 ms (25.08).
    """

    def odpal(self, outcomes, hedge=2, limit=1, slots=None):
        """outcomes: {godzina: [(ok, msg, tx), ...]} — kolejne wywołania po kolei."""
        self.cancelled, self.proby = [], []
        kolejki = {h: list(v) for h, v in outcomes.items()}
        lock = threading.Lock()

        def fake_register(slot, price, local_cfg, speculative=False):
            h = slot["start_utc"].hour
            with lock:
                ok, msg, tx = kolejki[h].pop(0) if kolejki[h] else (False, "409", "")
                self.proby.append(h)
            if ok:
                local_cfg["transaction_id"] = tx
            return ok, msg

        def fake_cancel(tx, _cfg):
            self.cancelled.append(tx)
            return True, "cancelled"

        buf = io.StringIO()
        with mock.patch.object(cp, "register_slot", side_effect=fake_register), \
                mock.patch.object(cp, "cancel_reservation", side_effect=fake_cancel), \
                mock.patch("sys.stdout", buf):
            cfg = self.cfg(hedge=hedge, max_per_run=limit, salvo=4)
            res, reg = cp.auto_register_new_slots(slots or self.slots(15, 17, 18, 19),
                                                 {}, cfg, set())
        return res, reg, cfg, buf.getvalue()

    def test_top_target_is_fired_twice(self):
        """order=latest -> 19:00 jest czołowe i to ono dostaje kopię."""
        _res, _reg, _cfg, _out = self.odpal({h: [(False, "409", "")] * 3
                                             for h in (15, 17, 18, 19)})
        self.assertEqual(self.proby.count(19), 2, "czołowy termin nie dostał drugiego zapisu")
        for h in (15, 17, 18):
            self.assertEqual(self.proby.count(h), 1, f"{h}:00 nie miało być powielone")

    def test_a_slow_loss_does_not_bury_a_fast_win(self):
        """SEDNO EKSPERYMENTU: jedna kopia dostaje 409, druga wygrywa — liczy się wygrana."""
        res, reg, _cfg, _out = self.odpal({
            19: [(False, "409", ""), (True, "OK", "tx19")],
            18: [(False, "409", "")], 17: [(False, "409", "")], 15: [(False, "409", "")],
        })
        self.assertIn("s19", reg)
        self.assertTrue(res["s19"][0], "porażka wolniejszej kopii nadpisała zwycięstwo szybszej")

    def test_hedge_off_changes_nothing(self):
        _res, _reg, _cfg, _out = self.odpal({h: [(False, "409", "")] * 3
                                             for h in (15, 17, 18, 19)}, hedge=1)
        self.assertEqual(sorted(self.proby), [15, 17, 18, 19])

    def test_copies_start_together_not_staggered(self):
        """Kopie mają być RÓWNOCZESNYMI losowaniami — odstęp rozsunąłby je bez sensu."""
        starty = []

        def fake_register(slot, price, local_cfg, speculative=False):
            if slot["start_utc"].hour == 19:
                starty.append(time.monotonic())
            time.sleep(0.03)
            return False, "409"

        with mock.patch.object(cp, "register_slot", side_effect=fake_register), \
                mock.patch("sys.stdout", io.StringIO()):
            cp.auto_register_new_slots(self.slots(15, 17, 18, 19), {},
                                       self.cfg(hedge=3, salvo=4, stagger=40), set())
        self.assertEqual(len(starty), 3)
        self.assertLess(max(starty) - min(starty), 0.02,
                        "kopie dostały odstęp, choć mają startować razem")

    def test_the_second_copy_simply_loses_the_seat(self):
        """Dlaczego nie ma obrony przed podwójną rezerwacją: limit miejsc wynosi 1.

        Gdy jedna kopia zapisze się skutecznie, druga z definicji dostaje 409 — serwer
        nie ma czego jej przydzielić. Zostaje tylko poprawne scalenie dwóch odpowiedzi
        na ten sam identyfikator (lepki sukces), co pilnuje osobny test.
        """
        res, reg, cfg, _out = self.odpal({
            19: [(True, "OK", "tx19"), (False, "No available seats", "")],
            18: [(False, "409", "")], 17: [(False, "409", "")], 15: [(False, "409", "")],
        })
        self.assertEqual(reg, {"s19"})
        self.assertTrue(res["s19"][0])
        self.assertEqual(self.cancelled, [], "nic nie powinno być anulowane")
        # Obie kopie zostają w dzienniku — to one są pomiarem rozrzutu.
        # (Etykieta `when` jest w czasie LOKALNYM, więc porównujemy po znaczniku kopii,
        # a nie po godzinie — inaczej test zależałby od strefy maszyny.)
        kopie = [s for s in cfg["shots"] if s["hedge"]]
        self.assertEqual(len(kopie), 2, "obie kopie muszą zostać w dzienniku")
        self.assertEqual(len({s["when"] for s in kopie}), 1, "kopie dotyczą jednej godziny")
        self.assertEqual(sorted(s["ok"] for s in kopie), [False, True])


class OwnDuplicateIsNotALostRaceTest(SalvoHelpers, unittest.TestCase):
    """Odbita kopia to nie przegrana. Log z 01.09:

        ! Auto-rejestracja nieudana dla wt 08.09 19:00: HTTP 409 "Booking is already exists"

    Termin był NASZ — to nasza druga kopia odbiła się od naszej własnej rezerwacji.
    Serwer używa innego komunikatu, gdy zajmuje go ktoś inny („No available seats"),
    więc te dwie sytuacje da się rozróżnić. Mylenie ich zamieniłoby każdy hedgowany
    dzień w fałszywy alarm „żadna rezerwacja się nie udała".
    """

    DUBLET = 'Decathlon HTTP 409: {"error":"Error","message":"Booking is already exists"}'
    RYWAL = 'Decathlon HTTP 409: {"error":"Error","message":"No available seats"}'

    def test_own_duplicate_is_labelled_as_ours(self):
        self.assertEqual(cp.skroc_powod(self.DUBLET), "miejsce już nasze")

    def test_a_real_lost_race_is_still_a_lost_race(self):
        self.assertEqual(cp.skroc_powod(self.RYWAL), "zajęty (409)")

    def test_own_duplicate_is_not_shouted_about(self):
        """Ostrzeżenie, które przychodzi codziennie, przestaje być czytane."""
        with mock.patch.object(cp, "register_slot",
                               side_effect=[(True, "accepted"), (False, self.DUBLET)]), \
                mock.patch("sys.stdout", io.StringIO()) as buf:
            cp.auto_register_new_slots(self.slots(19), {}, self.cfg(hedge=2, salvo=4), set())
            out = buf.getvalue()
        self.assertIn("miejsce już nasze", out)
        self.assertNotIn("Auto-rejestracja nieudana", out)

    def test_a_day_we_actually_hold_does_not_raise_the_alarm(self):
        """SEDNO: gdyby dublet liczył się jako przegrana, dziennik krzyczałby o dniu,
        w którym termin mamy w kieszeni."""
        cichy = cp.hunt_alert_reason({"registered": [], "failed": [
            {"when": "wt 08.09 19:00", "why": cp.skroc_powod(self.DUBLET)}],
            "aligned": True, "first_seen": "11:00:37"})
        self.assertEqual(cichy, "", "alarm o dniu, w którym termin jest nasz")
        # Prawdziwa przegrana nadal ma krzyczeć — nie stępiamy alarmu.
        glosny = cp.hunt_alert_reason({"registered": [], "failed": [
            {"when": "wt 08.09 19:00", "why": cp.skroc_powod(self.RYWAL)}],
            "aligned": True, "first_seen": "11:00:37"})
        self.assertIn("Żadna rezerwacja się nie udała", glosny)
        # Mieszanka: liczymy tylko realne porażki, nie odbite kopie.
        mieszane = cp.hunt_alert_reason({"registered": [], "failed": [
            {"when": "a", "why": cp.skroc_powod(self.DUBLET)},
            {"when": "b", "why": cp.skroc_powod(self.RYWAL)}],
            "aligned": True, "first_seen": "11:00:37"})
        self.assertIn("(1 prób)", mieszane)


class RemoteBatchesTest(RemoteHandlerTest):
    """PĘTLA PARTII — sedno straty z 01.09.

    Publikacja nie przychodzi naraz. Tego dnia grafik sypnął dwiema partiami w odstępie
    ~450 ms, a stara wersja kończyła się na PIERWSZEJ: sprint przestawał obserwować,
    rejestrował, wracał do domu, dodatek przetwarzał wynik i dopiero wtedy wołał
    Irlandię ponownie. Powstawało ~620 ms ślepoty dokładnie w kaskadzie publikacji —
    i w tym oknie 18:00 oraz 20:00 pojawiły się i zniknęły. Nie przegraliśmy ich
    w wyścigu; nie oddaliśmy w nie ANI JEDNEGO strzału.
    """

    def odpal_partie(self, partie, **nadpisz):
        """`partie`: lista dokumentów oddawanych przez kolejne rundy sprintu.

        `run_sprint` podstawiamy wprost — jego własne zachowanie ma osobne testy,
        a tutaj sprawdzamy JEDYNIE, czy handler zbiera kolejne partie.
        """
        self.zarejestrowane = []
        kolejka = list(partie)

        def fake_sprint(deadline, threads, url, baseline, tz, filters=None):
            while kolejka:
                doc = kolejka.pop(0)
                swiezy = {s["id"] for s in cp.free_slots(doc, self.LID,
                                                         datetime.now(timezone.utc))}
                if swiezy - set(baseline):
                    return (self.LID, doc, time.monotonic())
            return None

        def fake_register(slot, price, cfg, speculative=False):
            self.zarejestrowane.append(slot["date_id"])
            cfg["transaction_id"] = "tx-" + slot["date_id"]
            return True, "accepted"

        with mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: self.LID), \
                mock.patch.object(cp, "run_sprint", side_effect=fake_sprint), \
                mock.patch.object(cp, "register_slot", side_effect=fake_register), \
                mock.patch("sys.stdout", io.StringIO()):
            odp = self.handler.lambda_handler(
                self.zadanie(self.tresc(**nadpisz)), None)
        return self.rozpakuj(odp)

    def test_a_second_batch_is_caught_without_going_home(self):
        """SEDNO: 17:00 z drugiej partii ma zostać zdobyte w TYM SAMYM wywołaniu."""
        wynik = self.odpal_partie([self.doc(19), self.doc(19, 17)])
        self.assertEqual(sorted(self.zarejestrowane), ["d17", "d19"])
        self.assertEqual(wynik["timings"]["batches"], 2)
        self.assertEqual(len(wynik["registered"]), 2)

    def test_results_and_shots_from_every_batch_come_home(self):
        """Dziennik pokazujący tylko ostatnią partię kłamałby o całym polowaniu."""
        wynik = self.odpal_partie([self.doc(19), self.doc(19, 17), self.doc(19, 17, 20)])
        self.assertEqual(wynik["timings"]["batches"], 3)
        self.assertEqual(len(wynik["results"]), 3)
        self.assertEqual(len(wynik["shots"]), 3, "strzały z wcześniejszych partii zginęły")

    def test_the_freshest_document_travels_home(self):
        """Strona lokalna liczy z niego grafik dnia — musi dostać najpełniejszy obraz."""
        wynik = self.odpal_partie([self.doc(19), self.doc(19, 17, 20)])
        ids = {s["id"] for s in cp.free_slots(wynik["doc"], self.LID,
                                              datetime.now(timezone.utc))}
        self.assertEqual(len(ids), 3)

    def test_the_limit_covers_the_whole_call_not_each_batch(self):
        """NAJWAŻNIEJSZY BEZPIECZNIK: trzy partie przy limicie 2 to nadal 2 rezerwacje."""
        wynik = self.odpal_partie(
            [self.doc(19), self.doc(19, 17), self.doc(19, 17, 20)], max_per_run=2)
        self.assertEqual(len(self.zarejestrowane), 2, "limit liczony na partię, nie na wywołanie")
        self.assertEqual(len(wynik["registered"]), 2)

    def test_nothing_published_is_still_a_clean_answer(self):
        wynik = self.odpal_partie([])
        self.assertTrue(wynik["ok"])
        self.assertIsNone(wynik["doc"])
        self.assertEqual(wynik["timings"]["batches"], 0)

    def test_a_repeated_batch_does_not_book_twice(self):
        """Ten sam termin w kolejnej partii nie może dostać drugiego zapisu."""
        self.odpal_partie([self.doc(19), self.doc(19), self.doc(19, 17)])
        self.assertEqual(sorted(self.zarejestrowane), ["d17", "d19"])


class RemoteLossReachesTheJournalTest(HuntJournalTest):
    """REGRESJA 02.09: wpis twierdził „nigdy nie pokazane jako wolne: 17:00",
    a tego samego dnia oddaliśmy w 17:00 DWA strzały.

    `failed` powstawało wyłącznie z `new_slots` — z terminów, które strona lokalna
    NADAL widzi jako wolne. Termin przegrany w Irlandii jest już zajęty, gdy dokument
    wraca do domu, więc wypadał z `failed` i lądował w `never_seen`. A to jest dokładnie
    ta liczba, na podstawie której decydujemy, czy o daną godzinę w ogóle warto walczyć:
    „przegraliśmy wyścig" i „ta godzina nigdy nie jest publikowana" to dwa różne światy.
    """

    def strzal(self, godzina, ok=False, why="zajęty (409)"):
        return {"when": f"śr 09.09 {godzina}", "ok": ok, "ms": 178, "start_ms": 0,
                "seen_ms": 1, "salwa": True, "hedge": True, "why": "" if ok else why}

    def polowanie(self, shots, zajete):
        """Strzały są, ale `new_slots` puste — dokument wrócił bez tych terminów."""
        with mock.patch.object(cp, "notify_hunt"), mock.patch("sys.stdout", io.StringIO()):
            return cp.record_hunt(
                datetime(2026, 9, 2, 11, 0, 42, tzinfo=TZ), TZ,
                new_slots=[], wyniki={}, shots=shots,
                grid=("śr 09.09", 8, 11, zajete, date(2026, 9, 9)),
                zdalnie=True, topic="temat")

    def test_a_shot_proves_we_saw_the_hour(self):
        wpis = self.polowanie([self.strzal("17:00"), self.strzal("17:00")],
                              ["17:00", "18:00", "19:00"])
        self.assertNotIn("17:00", wpis["never_seen"],
                         "godzina, w którą strzelaliśmy, opisana jako nigdy nie widziana")
        self.assertEqual(wpis["never_seen"], ["18:00", "19:00"])

    def test_the_remote_loss_shows_up_as_a_loss(self):
        wpis = self.polowanie([self.strzal("17:00")], ["17:00"])
        self.assertEqual([f["when"] for f in wpis["failed"]], ["śr 09.09 17:00"])
        self.assertEqual(wpis["failed"][0]["why"], "zajęty (409)")

    def test_copies_of_one_shot_are_one_loss(self):
        """Cztery kopie w 20:00 to jedna przegrana godzina, nie cztery."""
        wpis = self.polowanie([self.strzal("20:00") for _ in range(4)], ["20:00"])
        self.assertEqual(len(wpis["failed"]), 1)

    def test_a_won_hour_is_never_reported_as_lost(self):
        """Jedna kopia przegrywa, druga wygrywa — to zdobycz, nie porażka."""
        wpis = self.polowanie(
            [self.strzal("19:00"), self.strzal("19:00", ok=True)], ["19:00"])
        self.assertEqual(wpis["failed"], [])
        self.assertNotIn("19:00", wpis["never_seen"])

    def test_hours_we_never_shot_at_are_still_flagged(self):
        """Nie stępiamy diagnostyki: godzina bez strzału nadal ma krzyczeć."""
        wpis = self.polowanie([self.strzal("20:00")], ["18:00", "19:00", "20:00"])
        self.assertEqual(wpis["never_seen"], ["18:00", "19:00"])


class SprintWindowCoversPublicationDriftTest(unittest.TestCase):
    """Pora publikacji przestała być stała: 11 dni (23.08–03.09) dało 11:00:13 … 11:00:42.

    Dziesięciosekundowe okno sprintu trafiało w 5 dni na 11. W pozostałe strzelaliśmy
    z domu, gdzie zapis trwa 448–1303 ms zamiast 66–178 ms z regionu — i to wystarczało,
    żeby stracić termin. Limity muszą więc pozwolić na okno rzędu 40 s po OBU stronach.
    """

    PORY = [13, 13, 15, 15, 25, 36, 36, 36, 37, 37, 42]   # sekunda po 11:00

    def pokrycie(self, start, dlugosc):
        return sum(1 for p in self.PORY if start <= p <= start + dlugosc)

    def test_the_old_window_missed_most_days(self):
        """Punkt odniesienia — bez tego nie widać, po co ta zmiana."""
        self.assertEqual(self.pokrycie(30, 10), 5)

    def test_the_new_default_covers_every_observed_day(self):
        self.assertEqual(self.pokrycie(5, 40), len(self.PORY))

    def test_remote_cap_allows_the_new_window(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "aws_remote"))
        self.addCleanup(lambda: sys.path.remove(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "aws_remote")))
        import handler
        self.assertGreaterEqual(handler.MAX_SPRINT_SEKUND, 40,
                                "Lambda przycięłaby okno i znów gubiła publikacje")


class BatchCountAlwaysVisibleTest(unittest.TestCase):
    """Brak licznika partii to JEDYNY sygnał, że Lambda chodzi na kodzie sprzed 0.20.0.

    Ukrywanie zera odbierało możliwość odróżnienia „stara wersja funkcji" od „nowa
    wersja, nic nie znalazła" — a dokładnie tego nie dało się odczytać z logu z 03.09.
    """

    def linia(self, timings):
        with mock.patch("sys.stdout", io.StringIO()) as buf:
            cp.adopt_remote({"timings": timings, "doc": None})
            return buf.getvalue()

    def test_zero_batches_is_reported_not_hidden(self):
        self.assertIn("0 partii", self.linia({"sprint_ms": 9956, "total_ms": 10394,
                                              "batches": 0}))

    def test_old_function_is_named_as_such(self):
        out = self.linia({"sprint_ms": 9956, "total_ms": 10394})
        self.assertIn("stara wersja funkcji", out)

    def test_batches_are_counted_in_polish(self):
        self.assertIn("2 partie", self.linia({"sprint_ms": 1, "total_ms": 2, "batches": 2}))
        self.assertIn("1 partia", self.linia({"sprint_ms": 1, "total_ms": 2, "batches": 1}))


class WatchWhileWritingTest(unittest.TestCase):
    """OBSERWATOR ZAPISU — sedno straty z 03.09.

    Czekanie na odpowiedź serwera trwało 1303 ms i przez cały ten czas nikt nie patrzył
    na grafik. W tym oknie liczba dostępnych skoczyła z 4 na 10 — sześć terminów
    pojawiło się, gdy byliśmy zajęci własnym strzałem. Gdy spojrzeliśmy ponownie, żaden
    nie pasował już do filtra, a przez 39 kolejnych sekund i 80 pobrań nie wróciła
    ani jedna wieczorna godzina.
    """

    LID = "kort"

    def slot(self, h, sid=None):
        return {"id": sid or f"s{h}", "date_id": f"d{h}", "name": "Rezerwacja godzinna",
                "start_utc": datetime(2026, 9, 10, h, 0, tzinfo=timezone.utc),
                "count": 0, "limit": 1, "price": None}

    def obserwuj(self, fale, znane=(), opoznienie=0.05):
        """`fale`: kolejne listy slotów oddawane przez kolejne pobrania."""
        kolejka, self.pobrania = list(fale), 0

        def fake_fetch(lid):
            self.pobrania += 1
            biezaca = kolejka.pop(0) if len(kolejka) > 1 else kolejka[0]
            return {"__sloty": biezaca}

        with mock.patch.object(cp, "fetch_listing", side_effect=fake_fetch), \
                mock.patch.object(cp, "free_slots", side_effect=lambda d, l, n: d["__sloty"]), \
                mock.patch.object(cp, "passes_filter", return_value=True):
            stop, wynik = cp.obserwuj_podczas_zapisu(self.LID, [], TZ, set(znane))
            time.sleep(opoznienie)      # tyle, ile trwa nasz zapis
            stop.set()
            time.sleep(0.05)            # daj wątkowi dokończyć
        return wynik

    def test_a_slot_appearing_during_the_write_is_caught(self):
        """SEDNO: 19:00 pojawia się, gdy czekamy na odpowiedź dla 18:00."""
        wynik = self.obserwuj([[self.slot(18)], [self.slot(18), self.slot(19)]],
                              znane={"s18"})
        self.assertIn("s19", wynik["nowe"])
        self.assertNotIn("s18", wynik["nowe"], "termin już znany zgłoszony jako nowy")

    def test_it_really_polls_more_than_once(self):
        """Jedno pobranie to nie obserwacja — okno zapisu trwa ponad sekundę."""
        wynik = self.obserwuj([[self.slot(18)]], znane={"s18"}, opoznienie=0.15)
        self.assertGreater(wynik["pobran"], 1)

    def test_it_stops_when_told(self):
        wynik = self.obserwuj([[self.slot(18)]], znane={"s18"})
        po_stopie = wynik["pobran"]
        time.sleep(0.1)
        self.assertEqual(wynik["pobran"], po_stopie, "wątek pobiera po zatrzymaniu")

    def test_it_keeps_the_freshest_document(self):
        """Grafik dnia liczony ze zdjęcia sprzed publikacji dał wpis „4 wolne z 4"."""
        wynik = self.obserwuj([[self.slot(18)], [self.slot(18), self.slot(19)]],
                              znane={"s18"})
        self.assertEqual(len(wynik["doc"]["__sloty"]), 2)

    def test_a_broken_fetch_does_not_kill_the_hunt(self):
        """Obserwacja jest dodatkiem — jej awaria nie może wywrócić polowania."""
        with mock.patch.object(cp, "fetch_listing", side_effect=RuntimeError("padło")):
            stop, wynik = cp.obserwuj_podczas_zapisu(self.LID, [], TZ, set())
            time.sleep(0.08)
            stop.set()
            time.sleep(0.05)
        self.assertEqual(wynik["nowe"], {})


class SecondWaveIsShotAtTest(unittest.TestCase):
    """Test od końca do końca: termin, który pojawia się W TRAKCIE naszego zapisu,
    musi dostać strzał w TYM SAMYM biegu — zanim ruszy księgowanie.

    03.09 księgowanie (dziennik, grafik, stan, kolejka powiadomień) zajęło ~470 ms,
    a kolejne pobranie następne ~530 ms. Razem z 1303 ms zapisu dawało to 2,3 s
    ślepoty. Cudzy zapis mieści się w tym z ogromnym zapasem.
    """

    LID = "aaaaaaaa-1111-2222-3333-444444444444"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for nazwa, sciezka in (("STATE_PATH", "s.json"), ("HUNTS_PATH", "h.json")):
            patcher = mock.patch.object(cp, nazwa, os.path.join(self.dir.name, sciezka))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.env = mock.patch.dict(os.environ, {
            "LISTINGS": f"https://go.decathlon.pl/l/{self.LID}",
            "NTFY_TOPIC": "", "FILTERS": "mon-sun:00:00-24:00",
            "AUTO_REGISTER": "true", "AUTO_REGISTER_DRY_RUN": "false",
            "AUTO_REGISTER_MAX": "5", "AUTO_REGISTER_SALVO": "0",
            "AUTO_REGISTER_HEDGE": "1", "AUTO_REGISTER_NAME": "Jan",
            "DECATHLON_TOKEN": jwt_with_exp(int(time.time()) + 3600),
            "CONFIG_PATH": os.path.join(self.dir.name, "brak.json"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        # Bez zapisanego stanu pierwszy bieg tylko ustala punkt odniesienia i NIC nie
        # rezerwuje — musimy więc udawać, że dodatek już wcześniej patrzył.
        with open(cp.STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"free_ids": ["cos-innego"], "registered_ids": []}, f)

    def doc(self, *godziny):
        start = datetime.now(timezone.utc) + timedelta(days=7)
        return {"data": {"attributes": {"title": "Kort", "price": None,
                                        "datesStats": {"availableListingDates": len(godziny)}}},
                "included": [TestFreeSlots.date_item(
                    start.replace(hour=h, minute=0, second=0, microsecond=0).isoformat(),
                    f"d{h}") for h in godziny]}

    def test_a_slot_published_during_our_write_gets_a_shot(self):
        strzelone = []

        def wolny_zapis(slot, price, cfg, speculative=False):
            strzelone.append(slot["start_utc"].hour)
            time.sleep(0.12)          # tu 03.09 minęło 1303 ms
            cfg["transaction_id"] = "tx"
            return False, "Decathlon HTTP 409: No available seats"

        # Pierwsze pobranie widzi 18:00. Kolejne — te z obserwatora — widzą też 19:00.
        widoki = [self.doc(18)] + [self.doc(18, 19)] * 50

        def fetch(lid):
            return widoki.pop(0) if len(widoki) > 1 else widoki[0]

        with mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: self.LID), \
                mock.patch.object(cp, "fetch_listing", side_effect=fetch), \
                mock.patch.object(cp, "register_slot", side_effect=wolny_zapis), \
                mock.patch("sys.stdout", io.StringIO()) as buf:
            cp.run_once(skip_light=True)
            out = buf.getvalue()

        self.assertIn(18, strzelone)
        self.assertIn(19, strzelone, "termin z drugiej fali nie dostał strzału")
        self.assertIn("Druga fala", out)

    def test_no_second_wave_means_no_extra_noise(self):
        """Gdy nic się nie pojawia, nie ma dodatkowych strzałów ani linii w logu."""
        strzelone = []

        def zapis(slot, price, cfg, speculative=False):
            strzelone.append(slot["start_utc"].hour)
            time.sleep(0.05)
            return False, "Decathlon HTTP 409: No available seats"

        with mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: self.LID), \
                mock.patch.object(cp, "fetch_listing", return_value=self.doc(18)), \
                mock.patch.object(cp, "register_slot", side_effect=zapis), \
                mock.patch("sys.stdout", io.StringIO()) as buf:
            cp.run_once(skip_light=True)
            out = buf.getvalue()

        self.assertEqual(strzelone, [18])
        self.assertNotIn("Druga fala", out)

    def test_outside_the_burst_we_do_not_poll_continuously(self):
        """Ciągłe pobieranie ma sens tylko w zrywie — poza nim to zbędny ruch."""
        pobrania = []

        def fetch(lid):
            pobrania.append(1)
            return self.doc(18)

        with mock.patch.object(cp, "resolve_current_id", side_effect=lambda x: self.LID), \
                mock.patch.object(cp, "fetch_listing", side_effect=fetch), \
                mock.patch.object(cp, "fetch_listing_light", return_value=self.doc(18)), \
                mock.patch.object(cp, "register_slot",
                                  side_effect=lambda *a, **k: (time.sleep(0.1), (False, "409"))[1]), \
                mock.patch("sys.stdout", io.StringIO()):
            cp.run_once(skip_light=False)
        self.assertLessEqual(len(pobrania), 2, "poza zrywem obserwator nie powinien działać")
