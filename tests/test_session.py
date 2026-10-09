"""Tests de la sesión de voz (issue #10): navegador y Deepgram simulados, con la secuencia de eventos real
observada en la API (2026-10-09). Sin red."""

import asyncio
import json

import pytest
from mcp.server.mcpserver import MCPServer

from server import events as ev
from server import session as S
from server.deepgram_agent import ErrorAgente
from server.mcp_hub import HubMCP

E = ev.EstadoConversacion


class AgenteFalso:
    """Imita ConexionAgente: responde a InjectUserMessage como Deepgram (turno con tool) y registra lo enviado."""

    def __init__(self, tool="buscar_ips", args='{"municipio": "Medellín"}'):
        self.tool, self.args = tool, args
        self.enviados: list[dict] = []
        self.audio = 0
        self.cerrado = False
        self.request_id = "falso"
        self._entrada: asyncio.Queue = asyncio.Queue()

    def empujar(self, *mensajes):
        for m in mensajes:
            self._entrada.put_nowait(m)

    async def enviar(self, m: dict):
        self.enviados.append(m)
        if m["type"] == "InjectUserMessage":
            self.empujar({"type": "UserStartedSpeaking"},
                         {"type": "ConversationText", "role": "user", "content": m["content"]},
                         {"type": "FunctionCallRequest", "functions": [
                             {"id": "fc_1", "name": self.tool, "arguments": self.args, "client_side": True}]})
        elif m["type"] == "FunctionCallResponse":
            self.empujar({"type": "ConversationText", "role": "assistant", "content": "Encontré 3 sedes."},
                         b"\x01\x00" * 480, {"type": "AgentAudioDone"})

    async def enviar_audio(self, pcm: bytes):
        self.audio += len(pcm)

    def __aiter__(self):
        return self._recibir()

    async def _recibir(self):
        while (m := await self._entrada.get()) is not None:
            yield m

    async def cerrar(self):
        self.cerrado = True
        self._entrada.put_nowait(None)

    def tipos_enviados(self) -> list[str]:
        return [m["type"] for m in self.enviados]


class WSFalso:
    """Imita el WebSocket del navegador de FastAPI."""

    def __init__(self):
        self.entrada: asyncio.Queue = asyncio.Queue()
        self.eventos: list = []
        self.audio: list[bytes] = []

    def texto(self, d: dict | str):
        self.entrada.put_nowait({"type": "websocket.receive", "text": d if isinstance(d, str) else json.dumps(d)})

    def binario(self, b: bytes):
        self.entrada.put_nowait({"type": "websocket.receive", "bytes": b})

    def cerrar(self):
        self.entrada.put_nowait({"type": "websocket.disconnect"})

    async def receive(self):
        return await self.entrada.get()

    async def send_text(self, t: str):
        self.eventos.append(ev.parse_servidor(t))

    async def send_bytes(self, b: bytes):
        self.audio.append(b)

    def de_tipo(self, tipo) -> list:
        return [e for e in self.eventos if isinstance(e, tipo)]

    def estados(self) -> list[tuple[str, int | None]]:
        return [(e.state.value, e.turn_id) for e in self.de_tipo(ev.EstadoEvento)]


def hub_de_prueba() -> HubMCP:
    mcp = MCPServer("prueba")

    async def buscar_ips(municipio: str = "") -> str:
        return f"Encontré 3 sedes en {municipio}."

    async def lenta() -> str:
        await asyncio.sleep(5)
        return "tarde"

    mcp.add_tool(buscar_ips)
    mcp.add_tool(lenta)
    hub = HubMCP()
    hub.registrar("prueba", mcp, cacheable=True)
    return hub


async def esperar(condicion, tope=3.0):
    async with asyncio.timeout(tope):
        while not condicion():
            await asyncio.sleep(0.01)


async def con_sesion(prueba, agente=None, falla: Exception | None = None):
    """Corre una sesión con navegador y Deepgram simulados; `prueba(ws, sesion, agente)` la maneja."""
    hub = hub_de_prueba()
    await hub.iniciar()
    agente = agente or AgenteFalso()
    abiertos = []

    async def abrir(funciones):
        if falla:
            raise falla
        abiertos.append(funciones)
        return agente

    ws = WSFalso()
    sesion = S.Sesion(ws, "s1", hub, abrir)
    tarea = asyncio.create_task(sesion.correr([ev.Ready(session_id="s1", voice="v", sources=[])]))
    try:
        await prueba(ws, sesion, agente)
        return ws, agente, abiertos
    finally:
        ws.cerrar()
        await asyncio.wait_for(tarea, 3)
        await hub.cerrar()


def correr(coro):
    return asyncio.run(coro)


# ------------------------------- turno completo -------------------------------

def test_turno_por_texto_con_tool_de_punta_a_punta():
    async def prueba(ws, sesion, agente):
        ws.texto({"type": "text_input", "text": "¿hospitales en Medellín?"})
        await esperar(lambda: ("inactivo", 1) in ws.estados())

    ws, agente, abiertos = correr(con_sesion(prueba))
    assert {f["name"] for f in abiertos[0]} == {"buscar_ips", "lenta"}  # las tools del hub van en Settings
    assert agente.tipos_enviados() == ["InjectUserMessage", "FunctionCallResponse"]
    respuesta = agente.enviados[1]
    assert respuesta["id"] == "fc_1" and respuesta["content"] == "Encontré 3 sedes en Medellín."
    assert ws.estados() == [("escuchando", 1), ("pensando", 1), ("ejecutando_tool", 1), ("pensando", 1),
                            ("hablando", 1), ("inactivo", 1)]
    tools = ws.de_tipo(ev.ToolCall)
    assert [t.status for t in tools] == ["running", "ok"] and tools[1].args == {"municipio": "Medellín"}
    assert tools[1].summary == "Encontré 3 sedes en Medellín." and tools[1].ms is not None
    assert [t.text for t in ws.de_tipo(ev.AgentText)] == ["Encontré 3 sedes."]
    hablantes = [(t.speaker, t.text) for t in ws.de_tipo(ev.Transcript)]
    assert hablantes == [("Hablante 1", "¿hospitales en Medellín?"), ("Agente", "Encontré 3 sedes.")]
    assert sum(len(b) for b in ws.audio) == 960
    assert isinstance(ws.eventos[0], ev.Ready)


def test_el_agente_se_abre_una_sola_vez_por_sesion():
    async def prueba(ws, sesion, agente):
        ws.texto({"type": "start"})
        ws.texto({"type": "text_input", "text": "uno"})
        await esperar(lambda: ("inactivo", 1) in ws.estados())
        ws.texto({"type": "text_input", "text": "dos"})
        await esperar(lambda: ("inactivo", 2) in ws.estados())

    _, agente, abiertos = correr(con_sesion(prueba))
    assert len(abiertos) == 1 and agente.tipos_enviados().count("InjectUserMessage") == 2


def test_el_microfono_solo_envia_audio_entre_start_y_stop():
    async def prueba(ws, sesion, agente):
        ws.binario(b"\x00" * 640)  # antes de start: se descarta
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.agente is not None)
        ws.binario(b"\x00" * 640)
        await esperar(lambda: agente.audio == 640)
        ws.texto({"type": "stop"})
        await asyncio.sleep(0.05)
        ws.binario(b"\x00" * 640)
        await asyncio.sleep(0.05)

    _, agente, _ = correr(con_sesion(prueba))
    assert agente.audio == 640


def test_sin_audio_envia_keepalive(monkeypatch):
    monkeypatch.setattr(S, "KEEPALIVE_S", 0.05)

    async def prueba(ws, sesion, agente):
        ws.texto({"type": "start"})
        await esperar(lambda: agente.tipos_enviados().count("KeepAlive") >= 2)

    correr(con_sesion(prueba))


# ------------------------------- interrupciones -------------------------------

def test_barge_in_vacia_el_audio_y_abre_turno():
    async def prueba(ws, sesion, agente):
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.agente is not None)
        agente.empujar(b"\x01\x00" * 100)  # el saludo empieza a sonar (turno 0)
        await esperar(lambda: ("hablando", 0) in ws.estados())
        agente.empujar({"type": "UserStartedSpeaking"})
        await esperar(lambda: ("escuchando", 1) in ws.estados())
        sesion.emitir(S._Audio(0, b"\x02\x00" * 100))  # audio del turno viejo que aún estaba en cola
        await asyncio.sleep(0.05)

    ws, _, _ = correr(con_sesion(prueba))
    assert ws.estados()[-2:] == [("interrumpido", 0), ("escuchando", 1)]
    assert [f.turn_id for f in ws.de_tipo(ev.AudioFlush)] == [0]
    assert all(b"\x02" not in b for b in ws.audio)  # el audio viejo no llegó al navegador


def test_function_call_cancelled_cancela_la_tool_y_no_responde():
    async def prueba(ws, sesion, agente):
        ws.texto({"type": "text_input", "text": "algo lento"})
        await esperar(lambda: any(t.status == "running" for t in ws.de_tipo(ev.ToolCall)))
        agente.empujar({"type": "UserStartedSpeaking"},
                       {"type": "FunctionCallCancelled", "functions": [{"id": "fc_1", "name": "lenta"}]})
        await esperar(lambda: any(t.status == "cancelled" for t in ws.de_tipo(ev.ToolCall)))

    ws, agente, _ = correr(con_sesion(prueba, AgenteFalso(tool="lenta", args="{}")))
    assert "FunctionCallResponse" not in agente.tipos_enviados()
    assert ws.estados()[-1][0] == "escuchando"


# ------------------------------- fallas -------------------------------

def test_si_deepgram_no_abre_avisa_y_la_sesion_sigue():
    async def prueba(ws, sesion, agente):
        ws.texto({"type": "text_input", "text": "hola"})
        await esperar(lambda: ws.de_tipo(ev.ErrorEvento))
        ws.texto("no es json")
        await esperar(lambda: len(ws.de_tipo(ev.ErrorEvento)) == 2)

    ws, _, _ = correr(con_sesion(prueba, falla=ErrorAgente("falta DEEPGRAM_API_KEY")))
    voz, cliente = ws.de_tipo(ev.ErrorEvento)
    assert voz.where == "voz" and voz.recoverable and "DEEPGRAM_API_KEY" in voz.message
    assert cliente.where == "cliente"


def test_si_deepgram_cierra_avisa_y_se_puede_reconectar():
    primero, segundo = AgenteFalso(), AgenteFalso()
    agentes = [primero, segundo]

    async def prueba(ws, sesion, _):
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.agente is primero)
        await primero.cerrar()
        await esperar(lambda: ws.de_tipo(ev.ErrorEvento))
        ws.texto({"type": "text_input", "text": "sigo aquí"})
        await esperar(lambda: ("inactivo", 1) in ws.estados())

    async def correr_con_dos():
        hub = hub_de_prueba()
        await hub.iniciar()

        async def abrir(_):
            return agentes.pop(0)

        ws = WSFalso()
        sesion = S.Sesion(ws, "s1", hub, abrir)
        tarea = asyncio.create_task(sesion.correr([]))
        try:
            await prueba(ws, sesion, None)
        finally:
            ws.cerrar()
            await asyncio.wait_for(tarea, 3)
            await hub.cerrar()
        return ws

    ws = correr(correr_con_dos())
    assert "Se cerró" in ws.de_tipo(ev.ErrorEvento)[0].message


def test_un_evento_raro_de_deepgram_no_corta_la_sesion():
    async def prueba(ws, sesion, agente):
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.agente is not None)
        agente.empujar({"type": "FunctionCallRequest", "functions": [None]}, {"type": "Desconocido"},
                       {"type": "ConversationText", "role": "user", "content": None})
        ws.texto({"type": "text_input", "text": "hola"})
        await esperar(lambda: ("inactivo", 1) in ws.estados())

    correr(con_sesion(prueba))


def test_deepgram_error_se_muestra_sin_cerrar():
    async def prueba(ws, sesion, agente):
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.agente is not None)
        agente.empujar({"type": "Error", "code": "FAILED_TO_THINK", "description": "Groq caído"})
        await esperar(lambda: ws.de_tipo(ev.ErrorEvento))

    ws, _, _ = correr(con_sesion(prueba))
    assert "Groq caído" in ws.de_tipo(ev.ErrorEvento)[0].message


def test_al_cerrar_el_navegador_se_cierra_deepgram():
    async def prueba(ws, sesion, agente):
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.agente is not None)

    _, agente, _ = correr(con_sesion(prueba))
    assert agente.cerrado


def test_la_difusion_llega_a_la_sesion():
    async def prueba(ws, sesion, agente):
        sesion.emitir(ev.SourceStatus(source="datos.gov.co", status="listo"))
        await esperar(lambda: ws.de_tipo(ev.SourceStatus))

    correr(con_sesion(prueba))


@pytest.mark.parametrize("argumentos,esperado", [('{"a": 1}', {"a": 1}), ("roto", {}), ("[1]", {}), (None, {})])
def test_args_para_ui(argumentos, esperado):
    assert S._args_para_ui(argumentos) == esperado
