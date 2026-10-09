"""Tests del esqueleto del servidor (issue #2): frontend, health y WebSocket /ws/voz."""

import time

import pytest
from fastapi.testclient import TestClient

from server import events as ev
from server import main
from server.main import app, componentes


from server.data import datos_gov


async def _catalogo_falso(cliente, on_status=None):
    """Catálogo mínimo sin red: los tests del servidor no dependen de datos.gov.co."""
    if on_status:
        await on_status(ev.SourceStatus(source="datos.gov.co", status="conectando"))
        await on_status(ev.SourceStatus(source="datos.gov.co", status="listo", rows=41427, total_rows=41427,
                                        progress=1))
    return datos_gov.Catalogo(total_filas=41427, municipios=[("CALI", "Cali")],
                              capacidades=[("CAMAS", "Intensiva Adultos")])


def _esperar_listo(c: TestClient) -> None:
    for _ in range(100):  # la carga del catálogo corre en segundo plano
        if c.get("/api/ready").status_code == 200:
            return
        time.sleep(0.02)
    raise AssertionError("el servidor no quedó listo")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main.datos_gov, "cargar_catalogo", _catalogo_falso)
    monkeypatch.setattr(main, "estado", main.Estado())
    componentes.clear()
    with TestClient(app) as c:
        _esperar_listo(c)
        yield c
    componentes.clear()


def test_sirve_el_frontend_en_la_raiz(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_sirve_el_contrato_para_el_frontend(client):
    r = client.get("/contrato.json")
    assert r.status_code == 200 and r.json()["estados"][0] == "inactivo"


def test_health_siempre_200_mientras_el_proceso_responde(client):
    componentes["brief"] = False
    try:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "degradado" and r.json()["componentes"]["brief"] is False
    finally:
        componentes.pop("brief")
    assert client.get("/api/health").json()["status"] == "ok"


def test_ready_503_hasta_que_los_componentes_esten_listos(client):
    assert client.get("/api/ready").status_code == 200
    componentes["dataset"] = False
    try:
        assert client.get("/api/ready").status_code == 503
    finally:
        componentes.pop("dataset")


@pytest.mark.parametrize("ruta", ["/.env", "/.gitkeep", "/sub/.secreto", "/docs", "/openapi.json"])
def test_no_expone_archivos_ocultos_ni_docs(client, ruta, tmp_path):
    assert client.get(ruta).status_code == 404


def test_static_no_sirve_dotfiles_aunque_existan(tmp_path):
    (tmp_path / ".env").write_text("SECRETO=1")
    (tmp_path / "index.html").write_text("<p>ok</p>")
    from fastapi import FastAPI
    mini = FastAPI()
    mini.mount("/", main.StaticSinOcultos(directory=tmp_path, html=True))
    with TestClient(mini) as c:
        assert c.get("/").status_code == 200
        assert c.get("/.env").status_code == 404


def test_ws_responde_ready_al_conectar(client):
    with client.websocket_connect("/ws/voz") as ws:
        evento = ev.parse_servidor(ws.receive_text())
        assert isinstance(evento, ev.Ready) and evento.session_id and evento.voice


def test_ws_mensaje_invalido_devuelve_error_y_sigue_abierto(client):
    with client.websocket_connect("/ws/voz") as ws:
        ws.receive_text()  # ready
        ws.receive_text()  # source_status de datos.gov.co
        ws.send_text("no es json")
        error = ev.parse_servidor(ws.receive_text())
        assert isinstance(error, ev.ErrorEvento) and error.where == "cliente" and error.recoverable
        ws.send_text('{"type": "start"}')
        ws.send_bytes(b"\x00\x00" * 320)  # audio: se acepta sin romper
        ws.send_text('{"type": "text_input", "text": "hola"}')
        eco = ev.parse_servidor(ws.receive_text())
        assert isinstance(eco, ev.AgentText) and eco.turn_id == 1 and "hola" in eco.text
        estado = ev.parse_servidor(ws.receive_text())
        assert isinstance(estado, ev.EstadoEvento) and estado.state == "inactivo" and estado.turn_id == 1
        ws.send_text('{"type": "text_input", "text": "otra"}')
        assert ev.parse_servidor(ws.receive_text()).turn_id == 2


def test_ws_error_inesperado_avisa_al_cliente_antes_de_cerrar(client, monkeypatch):
    def explota(_):
        raise RuntimeError("fallo interno")
    monkeypatch.setattr(main.ev, "parse_cliente_seguro", explota)
    with client.websocket_connect("/ws/voz") as ws:
        ws.receive_text()  # ready
        ws.receive_text()  # source_status de datos.gov.co
        ws.send_text('{"type": "stop"}')
        error = ev.parse_servidor(ws.receive_text())
        assert isinstance(error, ev.ErrorEvento) and error.where == "servidor" and not error.recoverable


def test_cada_conexion_tiene_su_sesion(client):
    with client.websocket_connect("/ws/voz") as a, client.websocket_connect("/ws/voz") as b:
        assert ev.parse_servidor(a.receive_text()).session_id != ev.parse_servidor(b.receive_text()).session_id


def test_al_arrancar_carga_el_catalogo_de_datos_gov(client):
    assert main.estado.catalogo is not None and main.estado.catalogo.total_filas == 41427
    assert main.estado.datos is not None
    assert client.get("/api/ready").json()["componentes"]["datos_gov"] is True


def test_ws_envia_el_estado_de_la_fuente_al_conectar(client):
    with client.websocket_connect("/ws/voz") as ws:
        assert isinstance(ev.parse_servidor(ws.receive_text()), ev.Ready)
        fuente = ev.parse_servidor(ws.receive_text())
        assert isinstance(fuente, ev.SourceStatus) and fuente.status == "listo" and fuente.rows == 41427


def test_si_datos_gov_no_responde_el_servidor_sigue_vivo_y_reintenta(monkeypatch):
    intentos = []

    async def falla(cliente, on_status=None):
        intentos.append(1)
        raise datos_gov.FuenteNoDisponible("datos.gov.co no responde (HTTP 503)")
    monkeypatch.setattr(main.datos_gov, "cargar_catalogo", falla)
    monkeypatch.setattr(main, "REINTENTO_DATOS_S", 0.05)
    monkeypatch.setattr(main, "estado", main.Estado())
    componentes.clear()
    with TestClient(app) as c:
        time.sleep(0.3)
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/health").json()["status"] == "degradado"
        assert c.get("/api/ready").status_code == 503
    assert len(intentos) >= 2  # siguió reintentando en segundo plano
    componentes.clear()
