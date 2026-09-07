#!/usr/bin/env python3
"""Testy zbieracza tokenów. `python3 -m unittest -v test_zbieracz`

Cała poprawność wielokontowości siedzi w decyzji „które konto odwiedzić teraz".
Pomyłka tutaj nie krzyczy — po prostu w sekundzie publikacji strzela pięć kont zamiast
dziesięciu, a dziennik pokazuje dziesięć prób.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "padel_browser"))
import zbieracz as zb  # noqa: E402

TERAZ = 1_800_000_000
KWADRANS = 15 * 60


def konta(*ids, glowne="glowne"):
    out = [{"id": glowne, "main": True}]
    out += [{"id": i, "main": False} for i in ids]
    return out


class WyborKontaTest(unittest.TestCase):
    """Strona odnawia JWT dopiero PO wygaśnięciu — odwiedzanie konta z żywym tokenem
    oddaje ten sam token i jest czystą stratą czasu."""

    def test_a_live_token_is_left_alone(self):
        """SEDNO HARMONOGRAMU: nie ma po co odwiedzać konta, które ma żywy token."""
        status = {"marek": {"exp": TERAZ + 600, "odwiedzone": TERAZ - 1000}}
        self.assertIsNone(zb.nastepne_konto(status, konta("marek"), TERAZ))

    def test_an_expired_token_is_picked_up(self):
        status = {"marek": {"exp": TERAZ - 10, "odwiedzone": TERAZ - 1000}}
        wybrane = zb.nastepne_konto(status, konta("marek"), TERAZ)
        self.assertEqual(wybrane["id"], "marek")

    def test_an_unknown_account_is_the_most_urgent(self):
        """Konto bez wpisu może być wylogowane — trzeba to sprawdzić, zanim zacznie się
        polowanie, a nie dowiedzieć się o tym w sekundzie publikacji."""
        status = {"marek": {"exp": TERAZ - 500, "odwiedzone": TERAZ - 1000}}
        wybrane = zb.nastepne_konto(status, konta("marek", "ania"), TERAZ)
        self.assertEqual(wybrane["id"], "ania")

    def test_the_longest_dead_token_goes_first(self):
        status = {"a": {"exp": TERAZ - 60, "odwiedzone": 0},
                  "b": {"exp": TERAZ - 900, "odwiedzone": 0},
                  "c": {"exp": TERAZ - 300, "odwiedzone": 0}}
        self.assertEqual(zb.nastepne_konto(status, konta("a", "b", "c"), TERAZ)["id"], "b")

    def test_the_main_account_is_never_visited(self):
        """Konto główne ma własną, stale otwartą przeglądarkę — zbieracz jej nie dotyka."""
        status = {"glowne": {"exp": TERAZ - 900, "odwiedzone": 0}}
        self.assertIsNone(zb.nastepne_konto(status, konta(), TERAZ))

    def test_cooldown_stops_hammering_a_logged_out_account(self):
        """Konto wylogowane nigdy nie odda tokenu. Bez karencji zbieracz kręciłby się
        na nim w kółko i nie odwiedził pozostałych."""
        status = {"marek": {"exp": None, "odwiedzone": TERAZ - 10}}
        self.assertIsNone(zb.nastepne_konto(status, konta("marek"), TERAZ))

    def test_after_the_cooldown_it_tries_again(self):
        status = {"marek": {"exp": None, "odwiedzone": TERAZ - zb.COOLDOWN - 1}}
        self.assertEqual(zb.nastepne_konto(status, konta("marek"), TERAZ)["id"], "marek")


class CiszaPrzedPublikacjaTest(unittest.TestCase):
    """Start Chromium to ~20 s. Próba rozpoczęta tuż przed publikacją i tak by nie
    zdążyła, a zabrałaby procesor monitorowi dokładnie wtedy, gdy jest najpotrzebniejszy."""

    STATUS = {"marek": {"exp": TERAZ - 900, "odwiedzone": 0}}

    def test_no_browser_launch_right_before_publication(self):
        for ile_przed in (10, 60, 89):
            self.assertIsNone(
                zb.nastepne_konto(self.STATUS, konta("marek"), TERAZ,
                                  publikacja=TERAZ + ile_przed),
                f"uruchomiono przeglądarkę {ile_przed} s przed publikacją")

    def test_earlier_than_the_stop_is_fine(self):
        self.assertIsNotNone(zb.nastepne_konto(self.STATUS, konta("marek"), TERAZ,
                                               publikacja=TERAZ + 200))

    def test_after_the_publication_work_resumes(self):
        self.assertIsNotNone(zb.nastepne_konto(self.STATUS, konta("marek"), TERAZ,
                                               publikacja=TERAZ - 60))


class TozsamoscTest(unittest.TestCase):
    """Pomyłka w mapowaniu profil → konto daje dziesięć tokenów JEDNEGO konta.
    Wygląda jak działający system, przynosi jeden kort dziennie i nic nie krzyczy."""

    def test_first_read_accepts_and_remembers(self):
        status = {}
        self.assertTrue(zb.tozsamosc_sie_zgadza(status, "marek", "user-1"))
        zb.odnotuj(status, "marek", exp=TERAZ + KWADRANS, user_id="user-1")
        self.assertEqual(status["marek"]["user_id"], "user-1")

    def test_a_different_identity_is_refused(self):
        status = {"marek": {"user_id": "user-1"}}
        self.assertFalse(zb.tozsamosc_sie_zgadza(status, "marek", "user-2"))

    def test_the_same_identity_passes(self):
        status = {"marek": {"user_id": "user-1"}}
        self.assertTrue(zb.tozsamosc_sie_zgadza(status, "marek", "user-1"))

    def test_an_unknown_identity_does_not_block_the_harvest(self):
        """Nieudane sprawdzenie tożsamości to nie powód, żeby przestać zbierać."""
        self.assertTrue(zb.tozsamosc_sie_zgadza({"marek": {"user_id": "user-1"}},
                                                "marek", ""))


class OdnotowanieTest(unittest.TestCase):
    def test_a_failed_visit_still_counts_as_a_visit(self):
        """Bez tego karencja by nie działała i zbieracz dobijałby się do wylogowanego
        konta w nieskończoność."""
        status = zb.odnotuj({}, "marek", blad="brak tokenu", teraz=TERAZ)
        self.assertEqual(status["marek"]["odwiedzone"], TERAZ)
        self.assertEqual(status["marek"]["blad"], "brak tokenu")

    def test_a_successful_visit_clears_the_error(self):
        status = {"marek": {"blad": "brak tokenu", "odwiedzone": 0}}
        zb.odnotuj(status, "marek", exp=TERAZ + KWADRANS, teraz=TERAZ)
        self.assertEqual(status["marek"]["blad"], "")
        self.assertEqual(status["marek"]["exp"], TERAZ + KWADRANS)


class IleZywychTest(unittest.TestCase):
    """Bez tej liczby nie da się odróżnić „wielokontowość nie pomaga" od „strzelały
    trzy konta z dziesięciu, bo reszta miała martwe tokeny"."""

    def test_counts_only_tokens_alive_at_the_publication(self):
        status = {"a": {"exp": TERAZ + 600}, "b": {"exp": TERAZ - 10}, "c": {}}
        self.assertEqual(zb.ile_zywych(status, konta("a", "b", "c"), TERAZ), 2)  # a + główne

    def test_the_main_account_always_counts(self):
        self.assertEqual(zb.ile_zywych({}, konta(), TERAZ), 1)


if __name__ == "__main__":
    unittest.main()
