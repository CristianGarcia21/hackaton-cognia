"""Tests del intérprete de fechas dichas en voz (citas)."""

from datetime import date, datetime

import pytest

from server.tools import fechas as F

HOY = date(2026, 10, 9)  # viernes


@pytest.mark.parametrize("texto,esperado", [
    ("el lunes 12 a las 10 y media", datetime(2026, 10, 12, 10, 30)),
    ("lunes 12 a las 10 de la mañana", datetime(2026, 10, 12, 10, 0)),
    ("mañana a las 3 de la tarde", datetime(2026, 10, 10, 15, 0)),
    ("pasado mañana a las 9", datetime(2026, 10, 11, 9, 0)),
    ("el martes a las 4", datetime(2026, 10, 13, 16, 0)),          # "a las 4" en una agenda de día: tarde
    ("el 3 a las 8:15", datetime(2026, 11, 3, 8, 15)),             # el 3 ya pasó este mes: noviembre
    ("15 de octubre a las once", datetime(2026, 10, 15, 11, 0)),
    ("el viernes a las diez", datetime(2026, 10, 9, 10, 0)),       # hoy es viernes
    ("2026-10-20T14:00", datetime(2026, 10, 20, 14, 0)),
])
def test_interpreta_lo_que_dice_el_usuario(texto, esperado):
    assert F.interpretar(texto, HOY) == esperado


@pytest.mark.parametrize("texto", ["cuando puedas", "el lunes", "a las 10"])
def test_si_falta_dia_u_hora_lo_dice(texto):
    with pytest.raises(F.FechaNoEntendida):
        F.interpretar(texto, HOY)


def test_dia_solo():
    assert F.interpretar_dia("el lunes 12", HOY) == date(2026, 10, 12)
    assert F.interpretar_dia("mañana", HOY) == date(2026, 10, 10)
