"""Tests de emociones y política de adaptación (issue #13, spec §9.2-9.3). Sin red: el LLM se simula."""

import asyncio

import pytest

from server.cognition import adaptation as A
from server.cognition import emotions as M


def emo(emocion="neutral", sentimiento=0.0, intensidad=0.5, senales=None):
    return M.Emocion(emocion=emocion, sentimiento=sentimiento, intensidad=intensidad, senales=senales or [])


def estado(*emos):
    e = M.EstadoAfectivo()
    for x in emos:
        e.agregar(x)
    return e


@pytest.mark.parametrize("crudo,esperado", [({"sentimiento": 8, "emocion": "Miedo", "intensidad": 7},
                                             (0.8, "ansiedad", 0.7)),
                                            ({"sentimiento": "x", "emocion": "rarísima", "intensidad": None},
                                             (0.0, "neutral", 0.0)),
                                            ({"sentimiento": -3.5, "emocion": "FRUSTRACIÓN", "intensidad": 0.4},
                                             (-0.35, "frustracion", 0.4))])
def test_la_salida_del_llm_se_normaliza_en_vez_de_rechazarse(crudo, esperado):
    e = M.Emocion.model_validate(crudo)
    assert (round(e.sentimiento, 2), e.emocion, e.intensidad) == esperado


@pytest.mark.parametrize("texto", ["Mi mamá tiene un dolor en el pecho muy fuerte", "Mi hijo no puede respirar",
                                   "Se desmayó y no reacciona", "Es una emergencia"])
def test_riesgo_vital_se_detecta_sin_llm(texto):
    assert M.riesgo_vital(texto)


def test_pregunta_normal_no_es_riesgo():
    assert M.riesgo_vital("¿Cuántas camas de UCI hay en Cali?") == []


def test_si_el_llm_falla_la_urgencia_no_se_pierde(monkeypatch):
    async def caido(*a, **k):
        raise TimeoutError

    monkeypatch.setattr(M.llm, "astructured", caido)
    e = asyncio.run(M.analizar("mi papá tiene dolor en el pecho"))
    assert e.emocion == "urgencia" and e.intensidad >= 0.8 and "dolor en el pecho" in e.senales
    e = asyncio.run(M.analizar("hola"))
    assert e.emocion == "neutral"


def test_el_llm_usa_groq_con_tope_de_tiempo(monkeypatch):
    llamadas = {}

    async def falso(texto, schema, **kw):
        llamadas.update(kw)
        return schema(emocion="ansiedad", sentimiento=-0.3, intensidad=0.6)

    monkeypatch.setattr(M.llm, "astructured", falso)
    assert asyncio.run(M.analizar("tengo miedo")).emocion == "ansiedad"
    assert llamadas["model"] == M.MODELO and llamadas["total_timeout"] == M.TIMEOUT_S


def test_promedio_movil_de_los_ultimos_tres_turnos():
    e = estado(emo(sentimiento=-0.9), emo(sentimiento=0.0), emo(sentimiento=0.3), emo(sentimiento=0.6))
    assert round(e.sentimiento_medio, 2) == 0.3 and round(e.tendencia, 2) == 0.6


@pytest.mark.parametrize("emos,regla", [
    ([emo("urgencia", -0.5)], "urgencia"),
    ([emo("enojo", -0.2)], "frustracion"),
    ([emo("neutral", -0.6), emo("neutral", -0.6)], "frustracion"),  # sentimiento medio < -0.4
    ([emo("ansiedad", -0.2)], "ansiedad"),
    ([emo("confusion")], "confusion"),
    ([emo("alegria", 0.6)], "normal"),
    ([emo("neutral", -0.9), emo("calma", 0.5), emo("calma", 0.5)], "normal"),  # un turno aislado no manda
])
def test_tabla_de_reglas(emos, regla):
    r, motivo = A.decidir(estado(*emos))
    assert r.nombre == regla and motivo


def test_una_emocion_explicita_le_gana_al_promedio_negativo():
    assert A.decidir(estado(emo("frustracion", -0.8), emo("frustracion", -0.8), emo("confusion", 0.0)))[0].nombre         == "confusion"


def test_la_urgencia_tiene_prioridad_sobre_la_frustracion():
    assert A.decidir(estado(emo("frustracion", -0.8), emo("urgencia", -0.8)))[0].nombre == "urgencia"


def test_solo_actua_cuando_cambia_la_regla_y_la_directiva_reemplaza_la_anterior():
    p = A.Politica(voz="aura-2-celeste-es")
    assert p.evaluar(estado(emo("neutral"))) is None  # arranca en normal: nada que enviar
    cambio = p.evaluar(estado(emo("ansiedad", -0.3)))
    assert cambio.evento.rule == "ansiedad" and cambio.evento.active and cambio.evento.speed == 0.9
    prompt, speak = cambio.mensajes
    assert prompt["type"] == "UpdatePrompt" and prompt["prompt"].startswith(A.PREFIJO)
    assert speak == {"type": "UpdateSpeak", "speak": {"provider": {"type": "deepgram", "model": "aura-2-celeste-es",
                                                                   "speed": 0.9}}}
    assert p.evaluar(estado(emo("ansiedad", -0.4))) is None  # misma regla: no se reenvía
    vuelta = p.evaluar(estado(emo("calma", 0.4)))
    assert vuelta.evento.rule == "normal" and not vuelta.evento.active


def test_sin_cambio_de_velocidad_no_envia_update_speak():
    p = A.Politica(voz="v")
    cambio = p.evaluar(estado(emo("frustracion", -0.5)))  # velocidad 1.0 = la de normal
    assert [m["type"] for m in cambio.mensajes] == ["UpdatePrompt"]
