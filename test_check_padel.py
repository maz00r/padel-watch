#!/usr/bin/env python3
"""Testy jednostkowe silnika (bez sieci). Uruchomienie: python3 -m unittest -v test_check_padel"""

import io
import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
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
        hit = self.sprint([{"a"}, {"a"}, {"a", "b"}], baseline={"a"})
        self.assertIsNotNone(hit)
        lid, doc = hit
        self.assertEqual(lid, "kort")
        self.assertIn("b", doc["__ids"])     # dane oddane BEZ ponownego pobierania

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
        with mock.patch.object(cp, "token_from_file", side_effect=lambda: next(tokens)), \
                mock.patch.object(cp.time, "sleep"):
            self.assertEqual(cp.wait_for_fresher_token("stary"), "nowy")

    def test_wait_gives_up_after_attempts(self):
        with mock.patch.object(cp, "token_from_file", return_value="stary"), \
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
