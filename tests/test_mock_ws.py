"""Tests del mock de /ws/voz (issue #3): orden del guion y cumplimiento del contrato."""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from server import events as ev
from server import mock_ws

# Tipos de evento que pide el issue #3 (todos deben aparecer).
ORDEN_ISSUE = ["ready", "source_status", "brief", "state", "transcript", "tool", "agent_text", "emotion",
               "adaptation", "verification", "action", "trace", "audio_flush"]


@pytest.fixture
def ws():
    with TestClient(mock_ws.create_app(velocidad=0)) as client, client.websocket_connect("/ws/voz") as conexion:
        yield conexion


def recibir_hasta(ws, condicion) -> tuple[list[BaseModel], list[bytes]]:
    """Recibe eventos (validándolos con el contrato) hasta que `condicion(evento)` sea verdadera."""
    eventos, audio = [], []
    for _ in range(5000):  # tope: si la condición nunca se cumple, falla en vez de leer para siempre
        msg = ws.receive()
        if msg.get("bytes") is not None:
            audio.append(msg["bytes"])
            continue
        evento = ev.parse_servidor(msg["text"])
        eventos.append(evento)
        if condicion(evento):
            return eventos, audio
    pytest.fail("no llegó el evento esperado")


def fin_de_conexion(e) -> bool:
    return isinstance(e, ev.EstadoEvento) and e.state == ev.EstadoConversacion.INACTIVO


def test_conexion_emite_ready_carga_brief_y_estado(ws):
    eventos, audio = recibir_hasta(ws, fin_de_conexion)
    tipos = [e.type for e in eventos]
    assert tipos[0] == "ready" and tipos[-2:] == ["brief", "state"] and not audio
    cargas = [e for e in eventos if isinstance(e, ev.SourceStatus)]
    assert cargas[-1].status == "listo" and cargas[-1].progress == 1
    assert [c.rows for c in cargas] == sorted(c.rows for c in cargas)  # rows es acumulado


def test_guion_maria_en_orden_realista_y_con_audio(ws):
    recibir_hasta(ws, fin_de_conexion)
    ws.send_text('{"type": "start"}')
    eventos, audio = recibir_hasta(ws, lambda e: isinstance(e, ev.Lesson))

    assert set(ORDEN_ISSUE) <= {"ready", "source_status", "brief", *(e.type for e in eventos)}
    # Dentro de cada turno: pensando → (tools) → agent_text → verification → trace.
    for turno in {e.turn_id for e in eventos if isinstance(e, ev.AgentText)}:
        tipos = [e.type if not isinstance(e, ev.EstadoEvento) else e.state.value
                 for e in eventos if getattr(e, "turn_id", None) == turno]
        assert tipos.index("pensando") < tipos.index("agent_text") < tipos.index("trace")
        if "verification" in tipos:
            assert tipos.index("agent_text") < tipos.index("verification") < tipos.index("trace")
        if "tool" in tipos:
            assert tipos.index("pensando") < tipos.index("tool") < tipos.index("agent_text")
    hablantes = {e.speaker for e in eventos if isinstance(e, ev.Transcript)}
    assert {"Hablante 1", "Hablante 2", "Agente"} <= hablantes
    assert {e.kind for e in eventos if isinstance(e, ev.Action)} == {"cita", "evento", "excel"}

    bytes_por_frame = ev.AUDIO_SALIDA["sample_rate"] * mock_ws.FRAME_MS // 1000 * 2
    assert audio and all(len(f) == bytes_por_frame for f in audio)


def test_parciales_y_final_comparten_segment_id(ws):
    recibir_hasta(ws, fin_de_conexion)
    ws.send_text('{"type": "start"}')
    eventos, _ = recibir_hasta(ws, lambda e: isinstance(e, ev.Lesson))
    por_segmento: dict[str, list[ev.Transcript]] = {}
    for e in eventos:
        if isinstance(e, ev.Transcript):
            por_segmento.setdefault(e.segment_id, []).append(e)
    for segmento in por_segmento.values():
        assert [s.is_final for s in segmento] == [False] * (len(segmento) - 1) + [True]


def test_audio_flush_viene_despues_de_interrumpido(ws):
    recibir_hasta(ws, fin_de_conexion)
    ws.send_text('{"type": "start"}')
    eventos, _ = recibir_hasta(ws, lambda e: isinstance(e, ev.Lesson))
    i = next(i for i, e in enumerate(eventos) if isinstance(e, ev.AudioFlush))
    previo = eventos[i - 1]
    assert isinstance(previo, ev.EstadoEvento) and previo.state == ev.EstadoConversacion.INTERRUMPIDO
    assert previo.turn_id == eventos[i].turn_id


def test_mensaje_invalido_responde_error_recuperable_sin_cerrar(ws):
    recibir_hasta(ws, fin_de_conexion)
    ws.send_text("esto no es json")
    error, _ = recibir_hasta(ws, lambda e: isinstance(e, ev.ErrorEvento))
    assert error[-1].where == "cliente" and error[-1].recoverable
    ws.send_text(json.dumps({"type": "text_input", "text": "hola"}))
    eventos, _ = recibir_hasta(ws, lambda e: isinstance(e, ev.Trace))
    assert any(isinstance(e, ev.AgentText) and "hola" in e.text for e in eventos)


def test_audio_del_microfono_se_ignora(ws):
    recibir_hasta(ws, fin_de_conexion)
    ws.send_bytes(b"\x00\x00" * 320)
    ws.send_text(json.dumps({"type": "text_input", "text": "sigo aquí"}))
    eventos, _ = recibir_hasta(ws, lambda e: isinstance(e, ev.Trace))
    assert not any(isinstance(e, ev.ErrorEvento) for e in eventos)


def test_el_guion_solo_usa_modelos_del_contrato():
    for paso in [*mock_ws.CONEXION, *mock_ws.GUION_MARIA]:
        if isinstance(paso, BaseModel):
            assert type(paso) in ev.EVENTOS_SERVIDOR.values()
            assert ev.parse_servidor(ev.to_json(paso)) == paso


def test_health():
    with TestClient(mock_ws.create_app(velocidad=0)) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/api/ready").json()["ready"] is True
        assert client.get("/").status_code == 200  # sirve web/index.html
        assert client.get("/.gitkeep").status_code == 404  # nunca sirve archivos ocultos


def test_turn_id_crece_aunque_primero_llegue_un_text_input(ws):
    recibir_hasta(ws, fin_de_conexion)
    ws.send_text(json.dumps({"type": "text_input", "text": "¿Cuántas camas de UCI hay en Antioquia?"}))
    recibir_hasta(ws, lambda e: isinstance(e, ev.Trace))
    ws.send_text('{"type": "start"}')
    eventos, _ = recibir_hasta(ws, lambda e: isinstance(e, ev.Lesson))
    turnos = [e.turn_id for e in eventos if isinstance(e, ev.AgentText)]
    assert turnos == list(range(2, 2 + mock_ws.TURNOS_MARIA))


def test_endpoints_http_de_las_acciones():
    with TestClient(mock_ws.create_app(velocidad=0)) as client:
        assert client.get("/api/brief").json()["questions"]
        assert "BEGIN:VCALENDAR" in client.get("/api/calendario.ics").text
        assert client.get("/api/citas/excel").status_code == 501
