"""Tests del verificador QA en vivo (issue #15, spec §9.4). Sin red: el LLM se simula."""

import asyncio

import pytest

from server.cognition import verifier as V

TOOL = V.ResultadoTool("contar_capacidad", {"municipio": "Medellín"},
                       "Total: 669 camas de cuidado intensivo en 53 sedes de MEDELLÍN (Antioquia).")


def correr(coro):
    return asyncio.run(coro)


def llm_que_responde(monkeypatch, **veredicto):
    vistos = {}

    async def falso(contexto, schema, **kw):
        vistos.update(kw, contexto=contexto)
        return schema(**veredicto)

    monkeypatch.setattr(V.llm, "astructured", falso)
    return vistos


def test_sin_tools_ni_cifras_no_gasta_llm(monkeypatch):
    vistos = llm_que_responde(monkeypatch, estado="no_respaldado")
    v = correr(V.verificar(V.Turno("hola", [], "¿En qué municipio estás?")))
    assert v.estado == "fuera_de_datos_ok" and not vistos


def test_cifras_sin_tools_si_se_verifican(monkeypatch):
    llm_que_responde(monkeypatch, estado="no_respaldado", problemas=["dio 300 camas sin consultar"],
                     correccion="No consulté los datos todavía.")
    v = correr(V.verificar(V.Turno("camas en Cali", [], "En Cali hay 300 camas.")))
    assert v.estado == "no_respaldado" and v.correccion


def test_el_contexto_lleva_pregunta_tools_y_respuesta(monkeypatch):
    vistos = llm_que_responde(monkeypatch, estado="respaldado")
    v = correr(V.verificar(V.Turno("¿UCI en Medellín?", [TOOL], "Hay 669 camas de cuidado intensivo.")))
    assert v.estado == "respaldado"
    assert "669 camas" in vistos["contexto"] and "¿UCI en Medellín?" in vistos["contexto"]
    assert vistos["model"] == V.MODELO and vistos["total_timeout"] == V.TIMEOUT_S


def test_la_correccion_solo_se_conserva_si_no_esta_respaldado(monkeypatch):
    llm_que_responde(monkeypatch, estado="parcial", correccion="algo")
    assert correr(V.verificar(V.Turno("x", [TOOL], "Hay 670 camas."))).correccion is None


def test_si_el_llm_falla_no_marca_nada(monkeypatch):
    async def caido(*a, **k):
        raise TimeoutError
    monkeypatch.setattr(V.llm, "astructured", caido)
    assert correr(V.verificar(V.Turno("x", [TOOL], "Hay 669 camas."))) is None


@pytest.mark.parametrize("crudo,esperado", [("No respaldado", "no_respaldado"), ("raro", "parcial"),
                                            ("fuera-de-datos-ok", "fuera_de_datos_ok")])
def test_estado_se_normaliza(crudo, esperado):
    assert V.Veredicto(estado=crudo).estado == esperado


def test_correccion_null_en_texto_es_none():
    assert V.Veredicto(estado="no_respaldado", correccion="null").correccion is None
