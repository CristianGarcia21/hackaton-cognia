"""Sesión de voz: una por WebSocket del navegador (spec §5.3). asyncio.TaskGroup + colas + máquina de estados.

    T1 receptor   lee el WebSocket del navegador: audio → cola de audio; start/stop/text_input
    T2 agente     WebSocket con Deepgram Voice Agent: envía audio (o KeepAlive) y despacha sus eventos
    T4 tools      una tarea por FunctionCallRequest → hub MCP → FunctionCallResponse (cancelable)
    T7 emisor     ÚNICA tarea que escribe en el WebSocket del navegador (cola de salida)

El agente de Deepgram se abre con el primer `start` o `text_input` (gesto del usuario: el navegador deja
reproducir el saludo) y queda abierto mientras dure la página; `stop` solo deja de enviar audio.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import websockets
from fastapi import WebSocket

from server import events as ev
from server import tools_registry
from server.deepgram_agent import ErrorAgente
from server.mcp_hub import HubMCP
from server.states import Cambio, Evento, Maquina

log = logging.getLogger("cognia.session")

E = ev.EstadoConversacion
MAX_AUDIO_EN_COLA = 50  # ~1-2 s de micrófono: si Deepgram se atrasa, se descarta lo más viejo
KEEPALIVE_S = 5.0       # sin audio (micrófono apagado o modo texto) Deepgram cierra tras ~10 s


class _Fin(Exception):
    """El navegador cerró: termina el TaskGroup."""


@dataclass
class _Audio:
    turn_id: int  # turno en el que llegó: si el usuario interrumpe, el audio viejo no se envía
    pcm: bytes


class Sesion:
    def __init__(self, ws: WebSocket, session_id: str, hub: HubMCP,
                 abrir_agente: Callable[[list[dict]], Awaitable]):
        self.ws, self.session_id, self.hub = ws, session_id, hub
        self._abrir_agente = abrir_agente  # funciones -> ConexionAgente (inyectable en tests)
        self.maquina = Maquina()
        self.agente = None
        self.microfono = False
        self.transcribir_usuario = True  # el STT diarizado (#11) lo apaga para no duplicar el panel
        self.latencias: dict[str, float] = {}  # último LatencyReport (para la traza, #19)
        self._salida: asyncio.Queue = asyncio.Queue()
        self._audio: asyncio.Queue[bytes] = asyncio.Queue(MAX_AUDIO_EN_COLA)
        self._tg: asyncio.TaskGroup | None = None
        self._tarea_agente: asyncio.Task | None = None
        self._listo = asyncio.Event()  # la conexión con Deepgram terminó de abrirse (bien o mal)
        self._tools: dict[str, asyncio.Task] = {}
        self._segmentos = 0

    # ------------------------------- API para main -------------------------------

    def emitir(self, evento) -> None:
        """Encola un evento para el navegador (no bloquea; lo escribe T7)."""
        self._salida.put_nowait(evento)

    async def correr(self, iniciales: list) -> None:
        for e in iniciales:
            self.emitir(e)
        try:
            async with asyncio.TaskGroup() as tg:
                self._tg = tg
                tg.create_task(self._emisor(), name="T7-emisor")
                tg.create_task(self._receptor(), name="T1-receptor")
        except* _Fin:
            pass
        finally:
            if self.agente is not None:
                await self.agente.cerrar()

    # ------------------------------- T7 emisor -------------------------------

    async def _emisor(self) -> None:
        while True:
            item = await self._salida.get()
            try:
                if isinstance(item, _Audio):
                    if item.turn_id == self.maquina.turn_id:  # audio de un turno interrumpido: se descarta
                        await self.ws.send_bytes(item.pcm)
                else:
                    await self.ws.send_text(ev.to_json(item))
            except Exception as e:  # noqa: BLE001 — el navegador se fue: cerrar la sesión sin ruido
                raise _Fin from e

    def _tarea(self, coro, nombre: str) -> asyncio.Task:
        """Subtarea del grupo que NUNCA lo tumba: un bug en una tool o en un evento se registra y la
        sesión sigue (en un TaskGroup, cualquier excepción cancelaría todo)."""
        async def segura():
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Error en %s (sesión %s)", nombre, self.session_id)
        return self._tg.create_task(segura(), name=nombre)

    # ------------------------------- T1 receptor -------------------------------

    async def _receptor(self) -> None:
        while True:
            mensaje = await self.ws.receive()
            if mensaje["type"] == "websocket.disconnect":
                raise _Fin
            if mensaje.get("bytes") is not None:
                if self.microfono and self.agente is not None:
                    self._poner_audio(mensaje["bytes"])
                continue
            recibido = ev.parse_cliente_seguro(mensaje.get("text") or "")
            if isinstance(recibido, ev.ErrorEvento):
                self.emitir(recibido)
            elif isinstance(recibido, ev.Start):
                self.microfono = True
                self._asegurar_agente()
            elif isinstance(recibido, ev.Stop):
                self.microfono = False
            elif isinstance(recibido, ev.TextInput):
                self._asegurar_agente()
                await self._cuando_agente({"type": "InjectUserMessage", "content": recibido.text})

    def _poner_audio(self, pcm: bytes) -> None:
        if self._audio.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._audio.get_nowait()
        self._audio.put_nowait(pcm)

    # ------------------------------- T2 agente -------------------------------

    def _asegurar_agente(self) -> None:
        if self._tarea_agente is None or self._tarea_agente.done():
            self._listo = asyncio.Event()
            self._tarea_agente = self._tarea(self._agente(), "T2-agente")

    async def _cuando_agente(self, mensaje: dict) -> None:
        """Envía un mensaje a Deepgram en cuanto la conexión esté lista (sin bloquear al receptor)."""
        async def esperar_y_enviar(listo: asyncio.Event):
            await listo.wait()
            if self.agente is not None:
                await self.agente.enviar(mensaje)
        self._tarea(esperar_y_enviar(self._listo), "T2-inyectar")

    async def _agente(self) -> None:
        try:
            self.agente = await self._abrir_agente(tools_registry.funciones_agente(self.hub))
        except ErrorAgente as e:
            log.warning("Sesión %s sin voz: %s", self.session_id, e)
            self.emitir(ev.ErrorEvento(where="voz", message=f"La voz no está disponible: {e}", recoverable=True))
            self._listo.set()  # lo pendiente se descarta: no hay agente
            return
        log.info("Sesión %s conectada a Deepgram (%s)", self.session_id, self.agente.request_id)
        self._listo.set()
        envio = self._tarea(self._enviar_audio(), "T2-audio")
        try:
            async for m in self.agente:
                try:
                    if isinstance(m, bytes):
                        self._audio_del_agente(m)
                    else:
                        self._evento_del_agente(m)
                except Exception:  # noqa: BLE001 — un evento raro no corta la conversación
                    log.exception("Evento de Deepgram no procesado: %.200s", m if isinstance(m, dict) else "audio")
            self.emitir(ev.ErrorEvento(where="voz", message="Se cerró la conexión de voz; vuelve a activar "
                                       "el micrófono o escribe para reconectar", recoverable=True))
        finally:
            envio.cancel()
            await self.agente.cerrar()
            self.agente = None
            for t in self._tools.values():
                t.cancel()

    async def _enviar_audio(self) -> None:
        """Micrófono → Deepgram. Sin audio por KEEPALIVE_S (micrófono apagado, modo texto): KeepAlive."""
        while self.agente is not None:
            try:
                pcm = await asyncio.wait_for(self._audio.get(), KEEPALIVE_S)
            except TimeoutError:
                pcm = None
            agente = self.agente
            if agente is None:
                return
            try:
                if pcm is None:
                    await agente.enviar({"type": "KeepAlive"})
                else:
                    await agente.enviar_audio(pcm)
            except websockets.ConnectionClosed:
                return  # la conexión se cerró: la tarea del agente ya lo detectó y avisa a la UI

    def _audio_del_agente(self, pcm: bytes) -> None:
        if self.maquina.estado is not E.HABLANDO:
            self._cambio(self.maquina.aplicar(Evento.AGENTE_HABLA))
        self.emitir(_Audio(self.maquina.turn_id, pcm))

    def _evento_del_agente(self, d: dict) -> None:
        tipo = d.get("type")
        if tipo == "UserStartedSpeaking":
            self._cambio(self.maquina.aplicar(Evento.USUARIO_HABLA))
        elif tipo == "ConversationText":
            self._texto(d.get("role"), (d.get("content") or "").strip())
        elif tipo == "FunctionCallRequest":
            for f in d.get("functions", []):
                if f.get("client_side", True):
                    self._pedir_tool(f)
        elif tipo == "FunctionCallCancelled":
            for f in d.get("functions", []):
                if (t := self._tools.get(f.get("id"))) is not None:
                    t.cancel()
            self._cambio(self.maquina.aplicar(Evento.TOOL_CANCELADA))
        elif tipo == "AgentAudioDone":
            self._cambio(self.maquina.aplicar(Evento.AUDIO_TERMINADO))
        elif tipo == "LatencyReport":
            self.latencias.update({k: v for k, v in d.items() if k != "type"})
        elif tipo == "Error":
            log.warning("Deepgram Error en %s: %s", self.session_id, d)
            self.emitir(ev.ErrorEvento(where="voz", message=f"Deepgram: {d.get('description') or d.get('code')}",
                                       recoverable=True))
        elif tipo == "Warning":
            log.info("Deepgram Warning en %s: %s", self.session_id, d)

    def _texto(self, rol: str, texto: str) -> None:
        if not texto:
            return
        self._segmentos += 1
        turno = self.maquina.turn_id
        if rol == "user":
            self._cambio(self.maquina.aplicar(Evento.USUARIO_TERMINA))
            if self.transcribir_usuario:
                self.emitir(ev.Transcript(segment_id=f"u-{self._segmentos}", turn_id=turno, speaker="Hablante 1",
                                          text=texto, is_final=True))
        elif rol == "assistant":
            self._cambio(self.maquina.aplicar(Evento.AGENTE_HABLA))
            self.emitir(ev.AgentText(turn_id=turno, text=texto))
            self.emitir(ev.Transcript(segment_id=f"a-{self._segmentos}", turn_id=turno, speaker="Agente",
                                      text=texto, is_final=True))

    def _cambio(self, cambio: Cambio | None) -> None:
        if cambio is None:
            return
        if cambio.interrumpido:
            # Barge-in: la UI vacía su búfer YA; T7 descarta el audio del turno viejo que aún esté en cola.
            self.emitir(ev.EstadoEvento(state=E.INTERRUMPIDO, turn_id=cambio.turn_id - 1))
            self.emitir(ev.AudioFlush(turn_id=cambio.turn_id - 1))
        self.emitir(ev.EstadoEvento(state=cambio.estado, turn_id=cambio.turn_id))

    # ------------------------------- T4 tools -------------------------------

    def _pedir_tool(self, f: dict) -> None:
        self._cambio(self.maquina.aplicar(Evento.TOOL_PEDIDA))
        fid = f.get("id") or ""
        self._tools[fid] = self._tarea(self._ejecutar_tool(fid, f.get("name") or "", f.get("arguments")),
                                       f"T4-{f.get('name')}")

    async def _ejecutar_tool(self, fid: str, nombre: str, argumentos) -> None:
        turno = self.maquina.turn_id
        args = _args_para_ui(argumentos)
        self.emitir(ev.ToolCall(turn_id=turno, name=nombre, args=args, status="running"))
        try:
            r = await tools_registry.despachar(self.hub, nombre, argumentos)
            self.emitir(ev.ToolCall(turn_id=turno, name=nombre, args=args, status=r.status, ms=r.ms,
                                    summary=tools_registry.resumen(r.texto)))
            if self.agente is not None:
                await self.agente.enviar({"type": "FunctionCallResponse", "id": fid, "name": nombre,
                                          "content": r.texto})
        except asyncio.CancelledError:
            # FunctionCallCancelled o barge-in: Deepgram descarta una respuesta tardía; no se envía nada.
            self.emitir(ev.ToolCall(turn_id=turno, name=nombre, args=args, status="cancelled"))
        finally:
            self._tools.pop(fid, None)
            if not self._tools:
                self._cambio(self.maquina.aplicar(Evento.TOOL_RESPONDIDA))


def _args_para_ui(argumentos) -> dict:
    if isinstance(argumentos, dict):
        return argumentos
    try:
        d = json.loads(argumentos or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}
