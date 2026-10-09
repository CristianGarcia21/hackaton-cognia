"""Tests de la memoria de lecciones (issue #23, spec §10.3)."""

import asyncio

import pytest

from server import events as ev
from server.cognition import lessons as L


def correr(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("texto,nombres", [("No, dije Palmira", ["Palmira"]),
                                           ("me refiero a Santa Marta", ["Santa Marta"]),
                                           ("quise decir Tuluá, no Tulua", ["Tuluá"]),
                                           ("No, es en San Andrés de Tumaco", ["San Andrés de Tumaco"]),
                                           ("¿Cuántas camas hay en Cali?", []),
                                           ("no sé qué hacer", [])])
def test_detecta_correcciones_de_nombres(texto, nombres):
    assert L.correcciones(texto) == nombres


def test_agregar_sin_duplicados_y_persistente(tmp_path):
    db = tmp_path / "l.db"
    l = L.Lecciones(db)
    e = correr(l.agregar("keyterm", "Tuluá", "corrección del usuario"))
    assert isinstance(e, ev.Lesson) and e.kind == "keyterm" and e.content == "Tuluá"
    assert correr(l.agregar("keyterm", "Tuluá", "otra vez")) is None  # ya existía
    assert L.Lecciones(db).keyterms() == ["Tuluá"]  # otra instancia (otra sesión/arranque) la ve


def test_tipo_invalido_o_vacio_no_se_guarda(tmp_path):
    l = L.Lecciones(tmp_path / "l.db")
    assert correr(l.agregar("otra", "x", "qa")) is None and correr(l.agregar("regla", "  ", "qa")) is None
    assert l.todas() == []


def test_reglas_y_sinonimos_van_al_prompt_y_keyterms_no(tmp_path):
    l = L.Lecciones(tmp_path / "l.db")
    assert l.para_prompt() == ""
    correr(l.agregar("regla", "Di la fecha de corte.", "verificador"))
    correr(l.agregar("sinonimo", "«la U» = Hospital Universitario del Valle", "qa"))
    correr(l.agregar("keyterm", "Tuluá", "usuario"))
    p = l.para_prompt()
    assert "LECCIONES APRENDIDAS" in p and "Regla: Di la fecha de corte." in p and "Sinónimo: «la U»" in p
    assert "Tuluá" not in p and l.keyterms() == ["Tuluá"]
    assert [e.kind for e in l.eventos()] == ["regla", "sinonimo", "keyterm"]


def test_una_base_inaccesible_no_tumba_nada(tmp_path):
    carpeta = tmp_path / "es_un_directorio.db"
    carpeta.mkdir()
    l = L.Lecciones(carpeta)
    assert correr(l.agregar("keyterm", "X", "qa")) is None and l.todas() == [] and l.para_prompt() == ""
