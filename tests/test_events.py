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


def test_text_input_no_acepta_texto_vacio():
    with pytest.raises(ValidationError):
        ev.TextInput(text="   ")


def test_formato_de_audio_documentado():
    assert ev.AUDIO_ENTRADA == {"encoding": "linear16", "sample_rate": 16000, "channels": 1}
    assert ev.AUDIO_SALIDA == {"encoding": "linear16", "sample_rate": 24000, "channels": 1}


def test_estados_de_la_maquina():
    assert [e.value for e in ev.EstadoConversacion] == [
        "inactivo", "escuchando", "pensando", "ejecutando_tool", "hablando", "interrumpido"]


# ---------------------- Robustez ante valores reales (revisión QA) ----------------------

def test_ms_con_decimales_se_redondea():
    assert ev.ToolCall(turn_id=1, name="x", status="ok", ms=12.7).ms == 13
    assert ev.Span(stage="llm", ms=3.2).ms == 3


@pytest.mark.parametrize("valor,esperado", [(1.0000001, 1.0), (-3, -1.0), (0.25, 0.25)])
def test_sentimiento_se_recorta_al_rango(valor, esperado):
    e = ev.Emotion(speaker="Hablante 1", sentiment=valor, emotion="calma", intensity=2)
    assert e.sentiment == esperado and e.intensity == 1.0


@pytest.mark.parametrize("valor,esperado", [
    ("frustración", "frustracion"), ("Ansiedad", "ansiedad"), ("miedo", "ansiedad"), ("euforia", "neutral")])
def test_emocion_se_normaliza(valor, esperado):
    assert ev.Emotion(speaker="H", sentiment=0, emotion=valor, intensity=0.5).emotion == esperado


@pytest.mark.parametrize("valor,esperado", [("Respaldado", "respaldado"), ("raro", "parcial")])
def test_estado_de_verificacion_se_normaliza(valor, esperado):
    assert ev.Verification(turn_id=1, status=valor).status == esperado


def test_brief_recorta_a_5_preguntas_y_exige_al_menos_una():
    assert len(ev.Brief(summary="s", key_points=[], questions=list("abcdefg")).questions) == 5
    with pytest.raises(ValidationError):
        ev.Brief(summary="s", key_points=[], questions=[])


def test_progress_y_speed_se_recortan():
    assert ev.SourceStatus(source="d", status="descargando", progress=1.01).progress == 1.0
    assert ev.Adaptation(rule="ansiedad", style="s", speed=0.5, reason="r").speed == 0.7


def test_adaptation_active_se_deriva_de_la_regla():
    assert ev.Adaptation(rule="normal", style="s", speed=1, reason="r").active is False
    assert ev.Adaptation(rule="ansiedad", style="s", speed=1, reason="r").active is True


def test_transcript_tiene_segmento_tiempos_opcionales_y_end_mayor_o_igual():
    t = ev.Transcript(segment_id="s1", speaker="Agente", text="hola", is_final=True)
    assert t.start is None and t.end is None
    assert ev.Transcript(segment_id="s2", speaker="H", text="x", start=5, end=1, is_final=False).end == 5


def test_text_input_recorta_espacios_y_largo():
    assert ev.TextInput(text="  hola  ").text == "hola"
    assert len(ev.TextInput(text="a" * 5000).text) == 2000


@pytest.mark.parametrize("raw", ["no es json", "{}", '{"type": "ready"}', '{"type": "start", "x": 1}'])
def test_parse_cliente_seguro_devuelve_error_recuperable(raw):
    resultado = ev.parse_cliente_seguro(raw)
    assert isinstance(resultado, ev.ErrorEvento) and resultado.recoverable and resultado.where == "cliente"


def test_parse_cliente_seguro_devuelve_el_mensaje_si_es_valido():
    assert isinstance(ev.parse_cliente_seguro('{"type": "stop"}'), ev.Stop)


@pytest.mark.parametrize("modelo", list(ev.EVENTOS_SERVIDOR.values()), ids=lambda m: m.__name__)
def test_round_trip_por_json_de_todos_los_ejemplos(modelo):
    for ejemplo in ev.ejemplos(modelo):
        evento = modelo.model_validate(ejemplo)
        assert ev.parse_servidor(ev.to_json(evento)) == evento


def test_estado_se_serializa_como_texto():
    assert '"state":"pensando"' in ev.to_json(ev.EstadoEvento(state=ev.EstadoConversacion.PENSANDO))


def test_null_dentro_de_diccionarios_se_conserva():
    texto = ev.to_json(ev.ToolCall(turn_id=1, name="x", status="ok", args={"nivel": None}))
    assert json.loads(texto)["args"] == {"nivel": None}


def test_contrato_json_del_frontend_esta_actualizado():
    from pathlib import Path
    archivo = Path("web/contrato.json")
    assert archivo.exists(), "falta web/contrato.json: uv run python -m server.events --exportar"
    assert json.loads(archivo.read_text(encoding="utf-8")) == ev.contrato_para_frontend(), \
        "web/contrato.json desactualizado: uv run python -m server.events --exportar"


def test_cli_de_ejemplos_funciona():
    import subprocess, sys
    r = subprocess.run([sys.executable, "-m", "server.events"], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0 and '"type": "ready"' in r.stdout


# ---------------------- Segunda revisión QA: entradas basura ----------------------

@pytest.mark.parametrize("raw", ['{"type": "text_input", "text": null}', '{"type": "text_input", "text": 123}',
                                 '{"type": "text_input", "text": ["a"]}'])
def test_text_input_rechaza_no_strings(raw):
    assert isinstance(ev.parse_cliente_seguro(raw), ev.ErrorEvento)


@pytest.mark.parametrize("crear", [
    lambda: ev.Emotion(speaker="H", sentiment=None, emotion="calma", intensity=0.5),
    lambda: ev.Emotion(speaker="H", sentiment="mucho", emotion="calma", intensity=0.5),
    lambda: ev.Adaptation(rule="normal", style="s", speed=None, reason="r"),
    lambda: ev.Span(stage="x", ms=None),
    lambda: ev.Span(stage="x", ms=True),
    lambda: ev.Brief(summary="s", key_points=[], questions=None),
])
def test_none_o_basura_en_numeros_da_validation_error(crear):
    with pytest.raises(ValidationError):
        crear()


def test_nan_se_vuelve_neutro():
    assert ev.Emotion(speaker="H", sentiment=float("nan"), emotion="calma", intensity=0.5).sentiment == 0.0
    assert ev.Span(stage="x", ms=float("nan")).ms == 0


def test_brief_con_una_pregunta_como_texto():
    assert ev.Brief(summary="s", key_points=[], questions="¿Cuántas IPS hay?").questions == ["¿Cuántas IPS hay?"]
