"""Tests del esqueleto del servidor (issue #2): frontend, health y WebSocket /ws/voz."""

import time

import pytest
from fastapi.testclient import TestClient

from server import events as ev
from server import main
from server.main import app, componentes

ABRIR_AGENTE_REAL = main._abrir_agente  # el fixture lo reemplaza; el test del saludo usa el real


from server.data import datos_gov


async def _catalogo_falso(cliente, on_status=None):
    """Catálogo mínimo sin red: los tests del servidor no dependen de datos.gov.co."""
    if on_status:
        await on_status(ev.SourceStatus(source="datos.gov.co", status="conectando"))
        await on_status(ev.SourceStatus(source="datos.gov.co", status="listo", rows=41427, total_rows=41427,
                                        progress=1))
    return datos_gov.Catalogo(total_filas=41427, municipios=[("CALI", "Cali")],
                              capacidades=[("CAMAS", "Intensiva Adultos")])


BRIEF_FALSO = ev.Brief(summary="Registro de IPS de prueba.", key_points=["41.427 registros"],
                       questions=["¿Cuántas camas de UCI hay en Antioquia?", "¿Qué IPS hay en Cali?",
                                  "Necesito una cita en Cali"], stats={"registros": 41427})


async def _brief_falso(datos, catalogo):
    """Sin datos.gov.co ni Groq: el brief real se prueba en tests/test_brief.py."""
    from server.cognition.brief import Resultado
    return Resultado(BRIEF_FALSO, "Hola, soy Kognia. Te ayudo con las IPS de Colombia.", con_llm=False)


def _esperar_listo(c: TestClient) -> None:
    for _ in range(100):  # la carga del catálogo corre en segundo plano
        if c.get("/api/ready").status_code == 200:
            return
        time.sleep(0.02)
    raise AssertionError("el servidor no quedó listo")


async def _sin_deepgram(_funciones, _historial=None):
    """Los tests del servidor nunca abren el Deepgram real (los de la sesión usan uno simulado)."""
    from server.deepgram_agent import ErrorAgente
    raise ErrorAgente("Deepgram deshabilitado en los tests")


async def _sin_stt():
    from server.deepgram_stt import ErrorSTT
    raise ErrorSTT("STT deshabilitado en los tests")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_abrir_agente", _sin_deepgram)
    monkeypatch.setattr(main, "_abrir_stt", _sin_stt)
    monkeypatch.setattr(main.datos_gov, "cargar_catalogo", _catalogo_falso)
    monkeypatch.setattr(main.brief_fuente, "generar", _brief_falso)
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
        ws.receive_text()  # brief (#12)
        ws.send_text("no es json")
        error = ev.parse_servidor(ws.receive_text())
        assert isinstance(error, ev.ErrorEvento) and error.where == "cliente" and error.recoverable
        ws.send_text('{"type": "start"}')
        ws.send_bytes(bytes(640))  # audio: se acepta sin romper
        # Sin Deepgram: errores recuperables de voz y de transcripción (en cualquier orden); la sesión sigue.
        errores = [ev.parse_servidor(ws.receive_text()) for _ in range(2)]
        assert {e.where for e in errores} == {"voz", "transcripcion"} and all(e.recoverable for e in errores)
        ws.send_text("[]")
        assert ev.parse_servidor(ws.receive_text()).where == "cliente"


def test_ws_error_inesperado_avisa_al_cliente_antes_de_cerrar(client, monkeypatch):
    def explota(_):
        raise RuntimeError("fallo interno")
    monkeypatch.setattr(main.ev, "parse_cliente_seguro", explota)
    with client.websocket_connect("/ws/voz") as ws:
        ws.receive_text()  # ready
        ws.receive_text()  # source_status de datos.gov.co
        ws.receive_text()  # brief (#12)
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


def test_un_bug_en_el_catalogo_no_se_reintenta_para_siempre(monkeypatch):
    intentos = []

    async def bug(cliente, on_status=None):
        intentos.append(1)
        raise KeyError("n")
    monkeypatch.setattr(main.datos_gov, "cargar_catalogo", bug)
    monkeypatch.setattr(main, "REINTENTO_DATOS_S", 0.02)
    monkeypatch.setattr(main, "estado", main.Estado())
    componentes.clear()
    with TestClient(app) as c:
        time.sleep(0.2)
        assert c.get("/api/ready").status_code == 503
    assert len(intentos) == 1
    componentes.clear()


def test_difundir_encola_sin_esperar_a_ningun_navegador(monkeypatch):
    import asyncio

    class SesionFalsa:
        def __init__(self):
            self.recibidos = []

        def emitir(self, e):
            self.recibidos.append(e)

    a, b = SesionFalsa(), SesionFalsa()
    estado = main.Estado()
    estado.conexiones = {a, b}
    monkeypatch.setattr(main, "estado", estado)
    asyncio.run(main._difundir(ev.SourceStatus(source="datos.gov.co", status="listo")))
    assert len(a.recibidos) == len(b.recibidos) == 1


# ------------------------------- hub MCP (#9) -------------------------------

def test_al_arrancar_conecta_el_hub_mcp_con_las_tools_de_ips(client):
    assert client.get("/api/health").json()["componentes"]["mcp"] is True
    nombres = {t.nombre for t in main.estado.hub.tools()}
    assert {"describir_datos", "buscar_ips", "contar_capacidad", "detalle_ips"} <= nombres


def test_las_tools_usan_el_catalogo_cuando_termina_de_cargar(client):
    assert main.estado.ips.catalogo is main.estado.catalogo is not None


def test_si_el_hub_se_cae_health_queda_degradado(client):
    servidor = main.estado.hub._servidores["ips"]
    cliente, servidor.cliente = servidor.cliente, None
    try:
        r = client.get("/api/health").json()
        assert r["status"] == "degradado" and r["componentes"]["mcp"] is False
        assert client.get("/api/ready").status_code == 503
    finally:
        servidor.cliente = cliente


# ------------------------------- brief (#12) -------------------------------

def test_api_brief_devuelve_el_brief_en_cache(client):
    r = client.get("/api/brief")
    assert r.status_code == 200 and r.json()["summary"] == "Registro de IPS de prueba."
    assert len(r.json()["questions"]) == 3


def test_api_brief_503_mientras_se_calcula(client):
    main.estado.brief = None
    assert client.get("/api/brief").status_code == 503


def test_ws_envia_el_brief_al_conectar(client):
    with client.websocket_connect("/ws/voz") as ws:
        tipos = [ev.parse_servidor(ws.receive_text()).type for _ in range(3)]
    assert tipos[0] == "ready" and "brief" in tipos


def test_el_saludo_del_brief_va_al_greeting_del_agente(client, monkeypatch):
    capturado = {}

    async def abrir(_key, settings):
        capturado.update(settings)
        raise RuntimeError("no abrir de verdad")

    monkeypatch.setattr(main.ConexionAgente, "abrir", abrir)
    monkeypatch.setattr(main.config, "DEEPGRAM_API_KEY", "x")
    import asyncio
    with pytest.raises(RuntimeError):
        asyncio.run(ABRIR_AGENTE_REAL([]))
    assert capturado["agent"]["greeting"] == "Hola, soy Kognia. Te ayudo con las IPS de Colombia."
