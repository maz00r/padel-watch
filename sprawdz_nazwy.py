#!/usr/bin/env python3
"""Statyczny strażnik nieokreślonych nazw — mini-pyflakes na bibliotece standardowej.

Powód: 04.09 na produkcji poleciało `NameError("name 'cfg_startowy' is not defined")`
co 30 sekund, przez co kontrola sesji przed zrywem była martwa przez cały dzień.
Zmienna została wciągnięta do wydzielanej funkcji, a jej użycie zostało w `main`.

`py_compile` takiego błędu NIE wykrywa (składnia jest poprawna), a `main` nie ma
pokrycia testami, bo to nieskończona pętla. Ten skrypt zamyka lukę: przechodzi po
drzewie składni i sprawdza, czy każda odczytywana nazwa jest gdziekolwiek dostępna.

Zasada: wolimy przepuścić błąd niż podnieść fałszywy alarm. Strażnik, który krzyczy
bez powodu, zostaje wyłączony po tygodniu i wtedy nie chroni już przed niczym.
"""
import ast
import builtins
import sys

WBUDOWANE = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__"}
ZASIEGI = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _wezly_zasiegu(wezel):
    """Węzły należące do TEGO zasięgu — bez wnętrza funkcji zagnieżdżonych."""
    for pole in ast.iter_child_nodes(wezel):
        if isinstance(pole, ZASIEGI):
            yield pole                      # sama definicja tak, ciało nie
            continue
        yield pole
        yield from _wezly_zasiegu(pole)


def _argumenty(fn):
    a = getattr(fn, "args", None)
    if a is None:
        return set()
    return ({x.arg for x in (a.posonlyargs + a.args + a.kwonlyargs)}
            | {x.arg for x in (a.vararg, a.kwarg) if x})


def _wiazane(wezel):
    """Nazwy wprowadzane do tego zasięgu (przypisania, importy, def, except, global)."""
    nazwy = set()
    for n in _wezly_zasiegu(wezel):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            nazwy.add(n.id)
        elif isinstance(n, ZASIEGI) and hasattr(n, "name"):
            nazwy.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                nazwy.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            nazwy.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            nazwy.update(n.names)
    return nazwy


def sprawdz(sciezka):
    drzewo = ast.parse(open(sciezka, encoding="utf-8").read(), filename=sciezka)
    problemy = []

    def zejdz(wezel, widoczne, gdzie):
        wlasne = widoczne | _argumenty(wezel) | _wiazane(wezel)
        for n in _wezly_zasiegu(wezel):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in wlasne:
                problemy.append((n.lineno, n.id, gdzie))
        for n in _wezly_zasiegu(wezel):
            if isinstance(n, ZASIEGI):
                zejdz(n, wlasne, getattr(n, "name", "<lambda>"))

    zejdz(drzewo, WBUDOWANE, "<moduł>")
    return problemy


if __name__ == "__main__":
    zle = 0
    for plik in sys.argv[1:]:
        for linia, nazwa, gdzie in sorted(set(sprawdz(plik))):
            print(f"{plik}:{linia}: nieokreślona nazwa '{nazwa}' w {gdzie}()")
            zle += 1
    sys.exit(1 if zle else 0)
