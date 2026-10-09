"""Tests del brief de la fuente (issue #12). datos.gov.co y el LLM simulados: sin red."""

import asyncio

import pytest

from server import events as ev
from server.cognition import brief as B
from test_tools_ips import CATALOGO, DatosFalsos

REGLAS = [  # DatosFalsos usa la PRIMERA regla que coincide: las más específicas primero
    ("GROUP BY departamento ORDER", [{"departamento": "Antioquia", "cantidad": "900", "sedes": "1500"},
                               {"departamento": "Valle del cauca", "cantidad": "700", "sedes": "900"},
                               {"departamento": "Cali", "cantidad": "500", "sedes": "800"}]),
    ("GROUP BY nom_grupo_capacidad ORDER", [{"nom_grupo_capacidad": "CAMAS", "cantidad": "80000", "sedes": "5000"},
                                      {"nom_grupo_capacidad": "AMBULANCIAS", "cantidad": "4000", "sedes": "900"}]),
    ("count(*) AS sedes", [{"sedes": "15547"}]),
    ("IS NULL", [{"n": "25266"}]),
    ("GROUP BY naturaleza", [{"naturaleza": "Privada", "n": "25067"}, {"naturaleza": "Pública", "n": "16174"}]),
    ("fecha_corte", [{"fecha_corte": "Nov-2022"}]),
]


def correr(coro):
    return asyncio.run(coro)


def test_estadisticas_reales_unen_distritos_y_deduplican():
    datos = DatosFalsos(reglas=REGLAS)
    s = correr(B.estadisticas(datos, CATALOGO))
    assert s["registros"] == 41427 and s["sedes"] == 15547 and s["sin_nivel_pct"] == 61
    # Cali (distrito) suma al Valle del Cauca, como en las tools de IPS.
    primero, segundo = list(s["top_departamentos_por_sedes"].items())[:2]
    assert segundo[1] == 1500 and "valle" in primero[0].lower() and primero[1] == 1700
    assert s["capacidad_por_grupo"] == {"CAMAS": 80000, "AMBULANCIAS": 4000}
    assert any("|>" in q for q in datos.consultas)  # sumas sin duplicados (mismo SoQL que las tools)


def test_con_llm_completa_preguntas_y_usa_su_saludo(monkeypatch):
    async def llm_falso(prompt, schema, **kw):
        assert "41427" in prompt and kw["total_timeout"] == B.TIMEOUT_S
        return schema(resumen="Fuente de IPS.", puntos_clave=["Punto 1", "punto 1", "Punto 2"],
                      preguntas_sugeridas=["¿Cuántas IPS hay en Caldas?"],
                      saludo="Hola, soy Kognia. Pregúntame por las IPS de Colombia.")

    monkeypatch.setattr(B.llm, "astructured", llm_falso)
    r = correr(B.generar(DatosFalsos(reglas=REGLAS), CATALOGO))
    assert r.con_llm and isinstance(r.brief, ev.Brief)
    assert r.brief.key_points == ["Punto 1", "Punto 2"]  # sin repetidos
    assert 3 <= len(r.brief.questions) <= 5 and r.brief.questions[0] == "¿Cuántas IPS hay en Caldas?"
    assert r.saludo.startswith("Hola, soy Kognia")
    assert r.brief.stats["registros"] == 41427


def test_si_el_llm_falla_sale_el_respaldo_con_las_mismas_cifras(monkeypatch):
    async def llm_caido(*_a, **_k):
        raise TimeoutError("Groq no respondió")

    monkeypatch.setattr(B.llm, "astructured", llm_caido)
    r = correr(B.generar(DatosFalsos(reglas=REGLAS), CATALOGO))
    assert not r.con_llm
    assert "41.427 registros" in r.brief.summary and "15.547 sedes" in r.brief.summary
    assert any("61 %" in p for p in r.brief.key_points)
    assert 3 <= len(r.brief.questions) <= 5 and "Kognia" in r.saludo
    ev.parse_servidor(ev.to_json(r.brief))  # cumple el contrato


def test_si_datos_gov_no_responde_lanza_para_reintentar():
    with pytest.raises(B.datos_gov.FuenteNoDisponible):
        correr(B.generar(DatosFalsos(falla=True), CATALOGO))
