"""Tests del contrato del WebSocket /ws/voz (spec §6)."""

import json

import pytest
from pydantic import ValidationError

from server import events as ev

TIPOS_SERVIDOR = {
    "ready", "state", "audio_flush", "transcript", "agent_text", "emotion", "adaptation",
    "verification", "tool", "action", "trace", "brief", "source_status", "lesson", "error",
}
TIPOS_CLIENTE = {"start", "stop", "text_input"}


def test_hay_un_modelo_por_cada_evento_del_servidor():
    assert set(ev.EVENTOS_SERVIDOR) == TIPOS_SERVIDOR


def test_hay_un_modelo_por_cada_mensaje_del_cliente():
    assert set(ev.MENSAJES_CLIENTE) == TIPOS_CLIENTE


@pytest.mark.parametrize("modelo", [*ev.EVENTOS_SERVIDOR.values(), *ev.MENSAJES_CLIENTE.values()],
                         ids=lambda m: m.__name__)
def test_cada_modelo_tiene_ejemplos_validos(modelo):
    ejemplos = ev.ejemplos(modelo)
    assert ejemplos, f"{modelo.__name__} no tiene ejemplos"
    for ejemplo in ejemplos:
        assert modelo.model_validate(ejemplo).model_dump(mode="json", exclude_none=True) == ejemplo


@pytest.mark.parametrize("tipo", sorted(TIPOS_SERVIDOR))
def test_parse_servidor_elige_el_modelo_por_type(tipo):
    modelo = ev.EVENTOS_SERVIDOR[tipo]
    evento = ev.parse_servidor(json.dumps(ev.ejemplos(modelo)[0]))
    assert isinstance(evento, modelo)


@pytest.mark.parametrize("tipo", sorted(TIPOS_CLIENTE))
def test_parse_cliente_elige_el_modelo_por_type(tipo):
    modelo = ev.MENSAJES_CLIENTE[tipo]
    assert isinstance(ev.parse_cliente(json.dumps(ev.ejemplos(modelo)[0])), modelo)


def test_to_json_omite_opcionales_vacios_y_es_parseable():
    evento = ev.Verification(turn_id=3, status="respaldado")
    texto = ev.to_json(evento)
    assert json.loads(texto) == {"type": "verification", "turn_id": 3, "status": "respaldado", "issues": []}
    assert ev.parse_servidor(texto) == evento


def test_type_desconocido_falla():
    with pytest.raises(ValidationError):
        ev.parse_servidor('{"type": "inventado"}')


def test_campos_extra_fallan():
    with pytest.raises(ValidationError):
        ev.parse_cliente('{"type": "start", "otro": 1}')


@pytest.mark.parametrize("campos", [
    {"sentiment": 1.5, "intensity": 0.5},
    {"sentiment": 0.0, "intensity": -0.1},
])
def test_emocion_respeta_rangos(campos):
    with pytest.raises(ValidationError):
        ev.Emotion(turn_id=1, speaker="Hablante 1", emotion="calma", **campos)


def test_emocion_solo_acepta_las_emociones_de_la_spec():
    with pytest.raises(ValidationError):
        ev.Emotion(turn_id=1, speaker="Hablante 1", sentiment=0, emotion="euforia", intensity=0.5)


@pytest.mark.parametrize("preguntas", [["a", "b"], ["a", "b", "c", "d", "e", "f"]])
def test_brief_exige_entre_3_y_5_preguntas(preguntas):
    with pytest.raises(ValidationError):
        ev.Brief(summary="s", key_points=[], questions=preguntas)


def test_text_input_no_acepta_texto_vacio():
    with pytest.raises(ValidationError):
        ev.TextInput(text="   ")


def test_formato_de_audio_documentado():
    assert ev.AUDIO_ENTRADA == {"encoding": "linear16", "sample_rate": 16000, "channels": 1}
    assert ev.AUDIO_SALIDA == {"encoding": "linear16", "sample_rate": 24000, "channels": 1}


def test_estados_de_la_maquina():
    assert [e.value for e in ev.EstadoConversacion] == [
        "inactivo", "escuchando", "pensando", "ejecutando_tool", "hablando", "interrumpido"]
