"""Tests del esqueleto del servidor (issue #2): frontend, health y WebSocket /ws/voz."""

import pytest
from fastapi.testclient import TestClient

from server import events as ev
from server.main import app, componentes


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_sirve_el_frontend_en_la_raiz(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_sirve_el_contrato_para_el_frontend(client):
    r = client.get("/contrato.json")
    assert r.status_code == 200 and r.json()["estados"][0] == "inactivo"


def test_health_ok_cuando_todo_esta_listo(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_503_si_un_componente_no_esta_listo(client):
    componentes["dataset"] = False
    try:
        r = client.get("/api/health")
        assert r.status_code == 503
        assert r.json() == {"status": "iniciando", "componentes": {"dataset": False}}
    finally:
        componentes.pop("dataset")


def test_ws_responde_ready_al_conectar(client):
    with client.websocket_connect("/ws/voz") as ws:
        evento = ev.parse_servidor(ws.receive_text())
        assert isinstance(evento, ev.Ready) and evento.session_id and evento.voice


def test_ws_mensaje_invalido_devuelve_error_y_sigue_abierto(client):
    with client.websocket_connect("/ws/voz") as ws:
        ws.receive_text()  # ready
        ws.send_text("no es json")
        error = ev.parse_servidor(ws.receive_text())
        assert isinstance(error, ev.ErrorEvento) and error.where == "cliente" and error.recoverable
        ws.send_text('{"type": "start"}')
        ws.send_bytes(b"\x00\x00" * 320)  # audio: se acepta sin romper
        ws.send_text('{"type": "text_input", "text": "hola"}')
        respuesta = ev.parse_servidor(ws.receive_text())
        assert isinstance(respuesta, (ev.EstadoEvento, ev.AgentText, ev.ErrorEvento))


def test_cada_conexion_tiene_su_sesion(client):
    with client.websocket_connect("/ws/voz") as a, client.websocket_connect("/ws/voz") as b:
        assert ev.parse_servidor(a.receive_text()).session_id != ev.parse_servidor(b.receive_text()).session_id
