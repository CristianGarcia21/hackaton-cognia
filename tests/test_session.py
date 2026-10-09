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

    async def abrir(funciones, historial=None):
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


def test_si_deepgram_se_corta_reconecta_solo_con_el_historial(monkeypatch):
    monkeypatch.setattr(S, "ESPERA_RECONEXION_S", 0.01)
    primero, segundo = AgenteFalso(), AgenteFalso()
    agentes, historiales = [primero, segundo], []

    async def prueba(ws, sesion):
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.agente is primero)
        primero.empujar({"type": "History", "role": "user", "content": "Me llamo Ana",
                         "conversational_behavior": "default"})
        await esperar(lambda: sesion._historial)
        await primero.cerrar()  # Deepgram corta la conexión
        await esperar(lambda: sesion.agente is segundo)
        ws.texto({"type": "text_input", "text": "sigo aquí"})
        await esperar(lambda: ("inactivo", 1) in ws.estados())

    async def correr_con_dos():
        hub = hub_de_prueba()
        await hub.iniciar()

        async def abrir(_, historial=None):
            historiales.append(historial)
            return agentes.pop(0)

        ws = WSFalso()
        sesion = S.Sesion(ws, "s1", hub, abrir)
        tarea = asyncio.create_task(sesion.correr([]))
        try:
            await prueba(ws, sesion)
        finally:
            ws.cerrar()
            await asyncio.wait_for(tarea, 3)
            await hub.cerrar()
        return ws

    ws = correr(correr_con_dos())
    assert historiales == [[], [{"type": "History", "role": "user", "content": "Me llamo Ana"}]]
    assert "reconectando" in ws.de_tipo(ev.ErrorEvento)[0].message
    assert segundo.tipos_enviados()[0] == "InjectUserMessage"


def test_si_no_logra_reconectar_lo_dice_y_se_rinde(monkeypatch):
    monkeypatch.setattr(S, "ESPERA_RECONEXION_S", 0.001)
    primero = AgenteFalso()
    llamadas = []

    async def prueba(ws, sesion):
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.agente is primero)
        primero.empujar({"type": "History", "role": "user", "content": "hola"})
        await esperar(lambda: sesion._historial)
        await primero.cerrar()
        await esperar(lambda: any("No pude reconectar" in e.message for e in ws.de_tipo(ev.ErrorEvento)))

    async def correr_sin_red():
        hub = hub_de_prueba()
        await hub.iniciar()

        async def abrir(_, historial=None):
            llamadas.append(1)
            if len(llamadas) == 1:
                return primero
            raise ErrorAgente("sin red")

        ws = WSFalso()
        sesion = S.Sesion(ws, "s1", hub, abrir)
        tarea = asyncio.create_task(sesion.correr([]))
        try:
            await prueba(ws, sesion)
        finally:
            ws.cerrar()
            await asyncio.wait_for(tarea, 3)
            await hub.cerrar()

    correr(correr_sin_red())
    assert len(llamadas) == 1 + S.MAX_RECONEXIONES


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


# ------------------------------- revisión QA de #10 -------------------------------

def test_tool_pedida_y_cancelada_en_el_mismo_lote_no_queda_pendiente():
    async def prueba(ws, sesion, agente):
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.agente is not None)
        agente.empujar({"type": "UserStartedSpeaking"},
                       {"type": "ConversationText", "role": "user", "content": "uno"},
                       {"type": "FunctionCallRequest", "functions": [
                           {"id": "x1", "name": "lenta", "arguments": "{}", "client_side": True}]},
                       {"type": "FunctionCallCancelled", "functions": [{"id": "x1", "name": "lenta"}]})
        await esperar(lambda: not sesion._tools and ("escuchando", 1) in ws.estados()[-1:])
        ws.texto({"type": "text_input", "text": "dos"})  # el turno siguiente con tool fluye normal
        await esperar(lambda: ("inactivo", 2) in ws.estados())

    ws, _, _ = correr(con_sesion(prueba))
    assert ("pensando", 2) in ws.estados()


def test_audio_que_llega_tras_la_interrupcion_no_se_reproduce():
    async def prueba(ws, sesion, agente):
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.agente is not None)
        agente.empujar(b"\x01\x00" * 100)
        await esperar(lambda: ("hablando", 0) in ws.estados())
        agente.empujar({"type": "UserStartedSpeaking"}, b"\x02\x00" * 100,
                       {"type": "ConversationText", "role": "assistant", "content": "resto viejo"},
                       {"type": "ConversationText", "role": "user", "content": "otra cosa"})
        await esperar(lambda: ("pensando", 1) in ws.estados())

    ws, _, _ = correr(con_sesion(prueba))
    assert all(b"\x02" not in b for b in ws.audio) and ("hablando", 1) not in ws.estados()
    assert [t.turn_id for t in ws.de_tipo(ev.AgentText) if t.text == "resto viejo"] == [0]


def test_audio_pendiente_tiene_tope_si_el_navegador_es_lento(monkeypatch):
    monkeypatch.setattr(S, "MAX_AUDIO_PENDIENTE", 1000)
    sesion = S.Sesion(WSFalso(), "s1", None, None)
    for _ in range(5):
        sesion.emitir(S._Audio(0, b"\x00" * 400))
    sesion.emitir(ev.SourceStatus(source="datos.gov.co", status="listo"))  # los eventos nunca se descartan
    assert sesion._salida.qsize() == 3 and sesion._audio_pendiente == 800


def test_un_bug_al_abrir_deepgram_avisa_y_no_deja_mensajes_colgados():
    async def prueba(ws, sesion, agente):
        ws.texto({"type": "text_input", "text": "hola"})
        await esperar(lambda: ws.de_tipo(ev.ErrorEvento))
        await esperar(lambda: sesion._listo.is_set())

    ws, _, _ = correr(con_sesion(prueba, falla=RuntimeError("bug")))
    assert ws.de_tipo(ev.ErrorEvento)[0].where == "voz"


# ------------------------------- STT diarizado (#11) -------------------------------

from server import deepgram_stt as STT  # noqa: E402


def resultado(palabras, final=True):
    return {"type": "Results", "is_final": final, "channel": {"alternatives": [{"words": [
        {"word": w.lower(), "punctuated_word": w, "speaker": h, "start": i * 0.5, "end": i * 0.5 + 0.4}
        for i, (h, w) in enumerate(palabras)]}]}}


class STTFalso:
    def __init__(self):
        self.audio: list[bytes] = []
        self.keepalives = 0
        self.cerrado = False
        self._entrada: asyncio.Queue = asyncio.Queue()

    def empujar(self, *rs):
        for r in rs:
            self._entrada.put_nowait(r)

    async def enviar_audio(self, pcm):
        self.audio.append(pcm)

    async def keepalive(self):
        self.keepalives += 1

    def __aiter__(self):
        return self._recibir()

    async def _recibir(self):
        while (m := await self._entrada.get()) is not None:
            yield m

    async def cerrar(self):
        self.cerrado = True
        self._entrada.put_nowait(None)


async def con_stt(prueba, stt, falla=None):
    hub = hub_de_prueba()
    await hub.iniciar()
    agente = AgenteFalso()

    async def abrir(_, historial=None):
        return agente

    async def abrir_stt():
        if falla:
            raise falla
        return stt

    ws = WSFalso()
    sesion = S.Sesion(ws, "s1", hub, abrir, abrir_stt)
    tarea = asyncio.create_task(sesion.correr([]))
    try:
        await prueba(ws, sesion, agente)
        return ws
    finally:
        ws.cerrar()
        await asyncio.wait_for(tarea, 3)
        await hub.cerrar()


def test_segmentos_separa_por_hablante():
    segs = STT.segmentos(resultado([(0, "Hola,"), (0, "necesito"), (1, "Mejor"), (1, "Palmira."), (0, "Bueno.")]))
    assert [(s.hablante, s.texto) for s in segs] == [(0, "Hola, necesito"), (1, "Mejor Palmira."), (0, "Bueno.")]
    assert segs[0].inicio == 0 and segs[1].fin == 1.9
    assert STT.segmentos({"type": "Results"}) == [] and STT.segmentos(resultado([])) == []


def test_url_del_stt_tiene_diarizacion_y_keyterms():
    u = STT.url()
    assert "diarize_model=latest" in u and "language=es" in u and "interim_results=true" in u
    assert "keyterm=Cali" in u and "diarize=true" not in u


def test_panel_muestra_dos_hablantes_con_tiempos_y_parciales_que_se_reemplazan():
    stt = STTFalso()

    async def prueba(ws, sesion, agente):
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.stt is not None)
        stt.empujar(resultado([(0, "Hola")], final=False), resultado([(0, "Hola,"), (0, "necesito")]),
                    resultado([(1, "Mejor"), (1, "Palmira.")]))
        await esperar(lambda: len(ws.de_tipo(ev.Transcript)) == 3)

    ws = correr(con_stt(prueba, stt))
    t = ws.de_tipo(ev.Transcript)
    assert [(x.segment_id, x.speaker, x.text, x.is_final) for x in t] == [
        ("h-0-0", "Hablante 1", "Hola", False), ("h-0-0", "Hablante 1", "Hola, necesito", True),
        ("h-1-0", "Hablante 2", "Mejor Palmira.", True)]
    assert t[2].start == 0 and t[2].end == 0.9


def test_el_mismo_audio_va_al_agente_y_al_stt_aunque_el_agente_hable():
    stt = STTFalso()

    async def prueba(ws, sesion, agente):
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.stt is not None and sesion.agente is not None)
        ws.binario(b"\x07" * 640)
        await esperar(lambda: len(stt.audio) == 1 and agente.audio == 640)
        agente.empujar(b"\x01\x00" * 10)  # el agente habla: otra persona puede hablarle encima
        await esperar(lambda: sesion.maquina.estado is E.HABLANDO)
        ws.binario(b"\x07" * 640)
        await esperar(lambda: len(stt.audio) == 2)

    correr(con_stt(prueba, stt))
    assert stt.audio == [b"\x07" * 640] * 2


def test_el_eco_del_agente_no_aparece_como_hablante():
    stt = STTFalso()

    async def prueba(ws, sesion, agente):
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.stt is not None)
        agente.empujar({"type": "ConversationText", "role": "assistant",
                        "content": "En Palmira hay cuatro sedes con cuidado intensivo."})
        await esperar(lambda: ws.de_tipo(ev.AgentText))
        stt.empujar(resultado([(1, "Palmira"), (1, "hay"), (1, "cuatro"), (1, "sedes"), (1, "con"), (1, "cuidado")]),
                    resultado([(0, "Gracias,"), (0, "¿y"), (0, "en"), (0, "Cali?")]))
        await esperar(lambda: any(t.speaker.startswith("Hablante") for t in ws.de_tipo(ev.Transcript)))

    ws = correr(con_stt(prueba, stt))
    assert [t.text for t in ws.de_tipo(ev.Transcript) if t.speaker != "Agente"] == ["Gracias, ¿y en Cali?"]


def test_con_stt_la_voz_del_usuario_no_se_duplica_pero_el_texto_escrito_si_aparece():
    stt = STTFalso()

    async def prueba(ws, sesion, agente):
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.stt is not None)
        agente.empujar({"type": "UserStartedSpeaking"}, {"type": "ConversationText", "role": "user", "content": "dicho"})
        await esperar(lambda: ("pensando", 1) in ws.estados())
        ws.texto({"type": "text_input", "text": "escrito"})
        await esperar(lambda: ("inactivo", 2) in ws.estados())

    ws = correr(con_stt(prueba, stt))
    textos = [t.text for t in ws.de_tipo(ev.Transcript) if t.speaker != "Agente"]
    assert textos == ["escrito"]


def test_si_el_stt_falla_avisa_y_el_panel_usa_el_texto_del_agente():
    async def prueba(ws, sesion, agente):
        ws.texto({"type": "start"})
        await esperar(lambda: ws.de_tipo(ev.ErrorEvento))
        agente.empujar({"type": "UserStartedSpeaking"}, {"type": "ConversationText", "role": "user", "content": "dicho"})
        await esperar(lambda: ws.de_tipo(ev.Transcript))

    ws = correr(con_stt(prueba, None, falla=STT.ErrorSTT("sin red")))
    assert ws.de_tipo(ev.ErrorEvento)[0].where == "transcripcion"
    assert ws.de_tipo(ev.Transcript)[0].text == "dicho"


def test_al_cerrar_el_navegador_se_cierra_el_stt():
    stt = STTFalso()

    async def prueba(ws, sesion, agente):
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.stt is not None)

    correr(con_stt(prueba, stt))
    assert stt.cerrado


# ------------------------------- emociones y adaptación (#13) -------------------------------

from server.cognition import emotions as EMO  # noqa: E402


@pytest.fixture(autouse=True)
def _emocion_sin_llm(monkeypatch):
    """Ningún test de la sesión llama al LLM real de emociones."""
    async def neutral(texto):
        return EMO.Emocion()
    monkeypatch.setattr(S.emotions, "analizar", neutral)


def test_cada_turno_del_usuario_emite_emocion_y_adapta_al_agente(monkeypatch):
    async def ansioso(texto):
        return EMO.Emocion(emocion="ansiedad", sentimiento=-0.4, intensidad=0.7, senales=["miedo"])
    monkeypatch.setattr(S.emotions, "analizar", ansioso)

    async def prueba(ws, sesion, agente):
        ws.texto({"type": "text_input", "text": "tengo mucho miedo"})
        await esperar(lambda: ws.de_tipo(ev.Adaptation) and ("inactivo", 1) in ws.estados())

    ws, agente, _ = correr(con_sesion(prueba))
    e = ws.de_tipo(ev.Emotion)[0]
    assert (e.emotion, e.speaker, e.turn_id, e.signals) == ("ansiedad", "Hablante 1", 1, ["miedo"])
    a = ws.de_tipo(ev.Adaptation)[0]
    assert a.rule == "ansiedad" and a.active and a.speed == 0.9
    tipos = agente.tipos_enviados()
    assert "UpdatePrompt" in tipos and "UpdateSpeak" in tipos


def test_una_emocion_lenta_no_frena_la_respuesta(monkeypatch):
    async def lenta(texto):
        await asyncio.sleep(2)
        return EMO.Emocion()
    monkeypatch.setattr(S.emotions, "analizar", lenta)

    async def prueba(ws, sesion, agente):
        ws.texto({"type": "text_input", "text": "hola"})
        await esperar(lambda: ("inactivo", 1) in ws.estados(), tope=1.0)

    ws, _, _ = correr(con_sesion(prueba))
    assert not ws.de_tipo(ev.Emotion)


# ------------------------------- verificador (#15) -------------------------------

from server.cognition import verifier as VER  # noqa: E402


@pytest.fixture(autouse=True)
def _verificador_sin_llm(monkeypatch):
    async def nada(turno):
        return None
    monkeypatch.setattr(S.verifier, "verificar", nada)


def test_al_terminar_la_respuesta_se_verifica_contra_las_tools(monkeypatch):
    vistos = []

    async def verificar(turno):
        vistos.append(turno)
        return VER.Veredicto(estado="respaldado")
    monkeypatch.setattr(S.verifier, "verificar", verificar)

    async def prueba(ws, sesion, agente):
        ws.texto({"type": "text_input", "text": "¿hospitales en Medellín?"})
        await esperar(lambda: ws.de_tipo(ev.Verification))

    ws, agente, _ = correr(con_sesion(prueba))
    t = vistos[0]
    assert t.pregunta == "¿hospitales en Medellín?" and t.respuesta == "Encontré 3 sedes."
    assert [(x.nombre, x.resultado) for x in t.tools] == [("buscar_ips", "Encontré 3 sedes en Medellín.")]
    v = ws.de_tipo(ev.Verification)[0]
    assert (v.turn_id, v.status) == (1, "respaldado")
    assert "InjectAgentMessage" not in agente.tipos_enviados()


def test_no_respaldado_con_correccion_se_corrige_en_voz(monkeypatch):
    async def verificar(turno):
        return VER.Veredicto(estado="no_respaldado", problemas=["3 no coincide"], correccion="Son 4 sedes.")
    monkeypatch.setattr(S.verifier, "verificar", verificar)

    async def prueba(ws, sesion, agente):
        ws.texto({"type": "text_input", "text": "¿hospitales?"})
        await esperar(lambda: "InjectAgentMessage" in agente.tipos_enviados())

    ws, agente, _ = correr(con_sesion(prueba))
    inyectado = [m for m in agente.enviados if m["type"] == "InjectAgentMessage"][0]
    assert inyectado["message"] == "Corrijo lo anterior: Son 4 sedes."
    assert ws.de_tipo(ev.Verification)[0].correction == "Son 4 sedes."


def test_una_interrupcion_cancela_la_verificacion(monkeypatch):
    empezo = asyncio.Event()

    async def lenta(turno):
        empezo.set()
        await asyncio.sleep(5)
        return VER.Veredicto(estado="no_respaldado", correccion="x")
    monkeypatch.setattr(S.verifier, "verificar", lenta)

    async def prueba(ws, sesion, agente):
        ws.texto({"type": "text_input", "text": "¿hospitales?"})
        await asyncio.wait_for(empezo.wait(), 3)
        agente.empujar(b"\x01\x00" * 10)  # el agente vuelve a hablar...
        await esperar(lambda: sesion.maquina.estado is E.HABLANDO)
        agente.empujar({"type": "UserStartedSpeaking"})  # ...y el usuario lo interrumpe
        await esperar(lambda: sesion._verificador.done())

    ws, agente, _ = correr(con_sesion(prueba))
    assert not ws.de_tipo(ev.Verification) and "InjectAgentMessage" not in agente.tipos_enviados()


# ------------------------------- traza (#19) -------------------------------

from server.cognition import trace as TR  # noqa: E402


@pytest.fixture(autouse=True)
def _trazas_en_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(TR, "DIR_TRAZAS", tmp_path / "trazas")
    return tmp_path / "trazas"


def test_traza_del_turno_con_spans_contexto_y_jsonl(monkeypatch, _trazas_en_tmp):
    async def respaldado(turno):
        return VER.Veredicto(estado="respaldado")
    monkeypatch.setattr(S.verifier, "verificar", respaldado)

    async def prueba(ws, sesion, agente):
        ws.texto({"type": "text_input", "text": "¿hospitales en Medellín?"})
        await esperar(lambda: ws.de_tipo(ev.Trace) and ws.de_tipo(ev.Verification))
        agente.empujar({"type": "LatencyReport", "stt_latency": 0.2})  # de otro momento: no cambia la traza
        await esperar(lambda: list(_trazas_en_tmp.glob("*.jsonl")))

    ws, _, _ = correr(con_sesion(prueba))
    final = ws.de_tipo(ev.Trace)[-1]
    etapas = [s.stage for s in final.spans]
    assert final.turn_id == 1 and "tool:buscar_ips" in etapas and "verificador" in etapas and "emocion" in etapas
    tool = next(s for s in final.spans if s.stage == "tool:buscar_ips")
    assert "municipio=Medellín" in tool.detail and "caracteres" in tool.detail
    assert final.context["pregunta"] == "¿hospitales en Medellín?" and final.context["tools"] == ["buscar_ips"]
    assert final.context["verificacion"] == "respaldado" and final.context["adaptacion_vigente"] == "normal"
    lineas = (_trazas_en_tmp / "s1.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lineas) == 1 and json.loads(lineas[0])["turn_id"] == 1


def test_spans_de_tiempos_de_deepgram_y_del_backend():
    t = TR.Traza(3)
    t.latencias = {"stt_latency": 0.25, "ttt_tool_latency": 0.3}
    t.fin_usuario = 10.0
    t.tool("buscar_ips", 120, {"municipio": "Cali"}, "x" * 50, "ok")
    t.ultima_tool, t.primer_texto, t.primer_audio = 10.5, 10.9, 11.2
    spans = {s.stage: s.ms for s in t.spans()}
    assert spans == {"fin_turno": 250, "llm_decide": 300, "tool:buscar_ips": 120, "llm_redacta": 400,
                     "primer_audio": 1200}


# ------------------------------- resiliencia: Settings (#21) -------------------------------

from server import deepgram_agent as DA  # noqa: E402


def test_settings_cadena_de_llm_groq_segunda_key_y_respaldo_de_deepgram():
    s = DA.settings([{"name": "f"}], groq_key="k1", groq_key_2="k2")
    cadena = s["agent"]["think"]
    assert [c["provider"]["type"] for c in cadena] == ["groq", "groq", "open_ai"]
    assert cadena[1]["endpoint"]["headers"]["authorization"] == "Bearer k2"
    assert all(c["prompt"] == DA.PROMPT and c["functions"] == [{"name": "f"}] for c in cadena)
    assert "endpoint" not in cadena[2]  # gestionado por Deepgram: sin keys nuestras
    solo = DA.settings([], groq_key="k1", respaldo=False)["agent"]["think"]
    assert isinstance(solo, dict) and solo["provider"]["type"] == "groq"


def test_settings_con_historial_no_saluda_y_lleva_el_contexto():
    h = [{"type": "History", "role": "user", "content": "hola"}]
    agente = DA.settings([], groq_key="k", historial=h)["agent"]
    assert agente["context"] == {"messages": h} and "greeting" not in agente
    assert "greeting" in DA.settings([], groq_key="k")["agent"]


def test_un_texto_escrito_durante_la_reconexion_espera_y_se_envia(monkeypatch):
    monkeypatch.setattr(S, "ESPERA_RECONEXION_S", 0.3)
    primero, segundo = AgenteFalso(), AgenteFalso()
    agentes = [primero, segundo]

    async def prueba(ws, sesion):
        ws.texto({"type": "start"})
        await esperar(lambda: sesion.agente is primero)
        primero.empujar({"type": "History", "role": "user", "content": "hola"})
        await esperar(lambda: sesion._historial)
        await primero.cerrar()
        await esperar(lambda: ws.de_tipo(ev.ErrorEvento))  # reconectando…
        ws.texto({"type": "text_input", "text": "¿sigues ahí?"})  # llega en plena reconexión
        await esperar(lambda: "InjectUserMessage" in segundo.tipos_enviados())

    async def correr_con_dos():
        hub = hub_de_prueba()
        await hub.iniciar()

        async def abrir(_, historial=None):
            return agentes.pop(0)

        ws = WSFalso()
        sesion = S.Sesion(ws, "s1", hub, abrir)
        tarea = asyncio.create_task(sesion.correr([]))
        try:
            await prueba(ws, sesion)
        finally:
            ws.cerrar()
            await asyncio.wait_for(tarea, 3)
            await hub.cerrar()

    correr(correr_con_dos())
