"""Sesión de voz: una por WebSocket del navegador (spec §5.3). asyncio.TaskGroup + colas + máquina de estados.

    T1 receptor   lee el WebSocket del navegador: audio → cola de audio; start/stop/text_input
    T2 agente     WebSocket con Deepgram Voice Agent: envía audio (o KeepAlive) y despacha sus eventos
    T3 STT        WebSocket con Deepgram Listen (diarize): transcripción por hablante para el panel (#11)
    T4 tools      una tarea por FunctionCallRequest → hub MCP → FunctionCallResponse (cancelable)
    T5 emociones  por turno del usuario: emoción → política de adaptación → UpdatePrompt/UpdateSpeak (#13)
    T6 verificador al terminar cada respuesta: ¿está respaldada por las tools? → verification (+ corrección) (#15)
    T7 emisor     ÚNICA tarea que escribe en el WebSocket del navegador (cola de salida)

El agente de Deepgram se abre con el primer `start` o `text_input` (gesto del usuario: el navegador deja
reproducir el saludo) y queda abierto mientras dure la página; `stop` solo deja de enviar audio.
"""

import asyncio
import contextlib
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import websockets
from fastapi import WebSocket
from rapidfuzz import fuzz

from server import events as ev
from server import tools_registry
from server.deepgram_agent import ErrorAgente
from server.cognition import emotions
from server.cognition import verifier
from server.cognition.trace import Traza, ahora
from server.cognition.adaptation import Politica
from server.cognition.lessons import Lecciones, correcciones
from server.deepgram_agent import KEYTERMS as KEYTERMS_AGENTE
from server.deepgram_agent import listen as config_listen
from server.deepgram_stt import ErrorSTT, segmentos
from server.mcp_hub import HubMCP
from server.states import Cambio, Evento, Maquina

log = logging.getLogger("cognia.session")

E = ev.EstadoConversacion
MAX_AUDIO_EN_COLA = 50  # ~1-2 s de micrófono: si Deepgram se atrasa, se descarta lo más viejo
KEEPALIVE_S = 5.0       # sin audio (micrófono apagado o modo texto) Deepgram cierra tras ~10 s
ESPERA_RECONEXION_S = 0.5  # reconexión con Deepgram: 0.5, 1, 2, 4, 8 s
MAX_RECONEXIONES = 5
CONEXION_ESTABLE_S = 10.0   # una conexión que duró más que esto reinicia la cuenta de fallos
MAX_HISTORIAL = 40          # mensajes History que se reenvían al reconectar
MAX_AUDIO_PENDIENTE = 480_000  # ~10 s de TTS (PCM16 24 kHz): si el navegador va más lento, se descarta


class _Fin(Exception):
    """El navegador cerró: termina el TaskGroup."""


@dataclass
class _Audio:
    turn_id: int  # turno en el que llegó: si el usuario interrumpe, el audio viejo no se envía
    pcm: bytes


class Sesion:
    def __init__(self, ws: WebSocket, session_id: str, hub: HubMCP,
                 abrir_agente: Callable[[list[dict], list[dict]], Awaitable], abrir_stt: Callable[[], Awaitable] | None = None,
                 voz: str = "aura-2-celeste-es", lecciones: Lecciones | None = None):
        self.ws, self.session_id, self.hub = ws, session_id, hub
        self._abrir_agente = abrir_agente  # (funciones, historial) -> ConexionAgente (inyectable en tests)
        self._abrir_stt = abrir_stt  # () -> ConexionSTT con diarización; None = sin panel diarizado
        self.stt = None
        self._audio_stt: asyncio.Queue[bytes] = asyncio.Queue(MAX_AUDIO_EN_COLA)
        self._tarea_stt: asyncio.Task | None = None
        self._textos_pendientes = 0  # text_input aún sin su ConversationText: esos sí van al panel
        self._fragmento = 0  # fragmento del STT: parciales y final comparten segment_id
        self._dichos_agente: deque[str] = deque(maxlen=3)  # para reconocer su eco en el micrófono
        self.afecto = emotions.EstadoAfectivo()
        self.politica = Politica(voz)
        self.analizar_emocion = emotions.analizar  # inyectable en tests
        self._ultimo_hablante = "Hablante 1"  # del STT diarizado: a quién atribuir la emoción
        self.verificar = verifier.verificar  # inyectable en tests
        self.lecciones = lecciones  # memoria entre sesiones (#23); None = no aprende
        self._turnos: dict[int, verifier.Turno] = {}  # pregunta, tools y respuesta por turno (para T6)
        self._verificados: set[int] = set()
        self._verificador: asyncio.Task | None = None
        self._trazas: dict[int, Traza] = {}  # traza por turno (#19)
        self.modelo_llm = ""  # para el contexto de la traza (lo fija main)
        self.maquina = Maquina()
        self.agente = None
        self.microfono = False
        self.latencias: dict[str, float] = {}  # último LatencyReport (para la traza, #19)
        self._salida: asyncio.Queue = asyncio.Queue()
        self._audio_pendiente = 0  # bytes de TTS en _salida (los eventos JSON nunca se descartan)
        self._audio: asyncio.Queue[bytes] = asyncio.Queue(MAX_AUDIO_EN_COLA)
        self._tg: asyncio.TaskGroup | None = None
        self._tarea_agente: asyncio.Task | None = None
        self._listo = asyncio.Event()  # la conexión con Deepgram terminó de abrirse (bien o mal)
        self._historial: deque[dict] = deque(maxlen=MAX_HISTORIAL)  # History de Deepgram, para reconectar
        self._tools: dict[str, asyncio.Task] = {}
        self._segmentos = 0

    # ------------------------------- API para main -------------------------------

    def emitir(self, evento) -> None:
        """Encola un evento para el navegador (no bloquea; lo escribe T7)."""
        if isinstance(evento, _Audio):
            if self._audio_pendiente + len(evento.pcm) > MAX_AUDIO_PENDIENTE:
                log.warning("Sesión %s: navegador lento, se descarta audio TTS", self.session_id)
                return
            self._audio_pendiente += len(evento.pcm)
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
            if self.stt is not None:
                await self.stt.cerrar()

    # ------------------------------- T7 emisor -------------------------------

    async def _emisor(self) -> None:
        while True:
            item = await self._salida.get()
            if isinstance(item, _Audio):
                self._audio_pendiente -= len(item.pcm)
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
        tarea = self._tg.create_task(segura(), name=nombre)
        # Cancelada antes de empezar: la corrutina nunca corrió; cerrarla evita el "never awaited".
        tarea.add_done_callback(lambda t: coro.close() if t.cancelled() else None)
        return tarea

    # ------------------------------- T1 receptor -------------------------------

    async def _receptor(self) -> None:
        while True:
            mensaje = await self.ws.receive()
            if mensaje["type"] == "websocket.disconnect":
                raise _Fin
            if mensaje.get("bytes") is not None:
                pcm = mensaje["bytes"]
                if self.microfono and self.agente is not None:
                    _poner(self._audio, pcm)
                if self.microfono and self.stt is not None:
                    _poner(self._audio_stt, pcm)  # el mismo audio: el eco del agente se filtra por texto
                continue
            recibido = ev.parse_cliente_seguro(mensaje.get("text") or "")
            if isinstance(recibido, ev.ErrorEvento):
                self.emitir(recibido)
            elif isinstance(recibido, ev.Start):
                self.microfono = True
                self._asegurar_agente()
                self._asegurar_stt()
            elif isinstance(recibido, ev.Stop):
                self.microfono = False
            elif isinstance(recibido, ev.TextInput):
                self._textos_pendientes += 1
                self._asegurar_agente()
                await self._cuando_agente({"type": "InjectUserMessage", "content": recibido.text})

    # ------------------------------- T3 STT diarizado -------------------------------

    def _asegurar_stt(self) -> None:
        if self._abrir_stt is not None and (self._tarea_stt is None or self._tarea_stt.done()):
            self._tarea_stt = self._tarea(self._transcribir(), "T3-stt")

    async def _transcribir(self) -> None:
        try:
            self.stt = await self._abrir_stt()
        except ErrorSTT as e:
            log.warning("Sesión %s sin transcripción diarizada: %s", self.session_id, e)
            self.emitir(ev.ErrorEvento(where="transcripcion", message=f"Transcripción por hablante no disponible: "
                                       f"{e}. Se muestra la del agente.", recoverable=True))
            return
        envio = self._tarea(self._enviar_audio_stt(), "T3-audio")
        try:
            async for resultado in self.stt:
                try:
                    self._resultado_stt(resultado)
                except Exception:  # noqa: BLE001
                    log.exception("Resultado del STT no procesado")
        finally:
            envio.cancel()
            await self.stt.cerrar()
            self.stt = None

    async def _enviar_audio_stt(self) -> None:
        while (stt := self.stt) is not None:
            try:
                pcm = await asyncio.wait_for(self._audio_stt.get(), KEEPALIVE_S)
            except TimeoutError:
                pcm = None
            try:
                await (stt.keepalive() if pcm is None else stt.enviar_audio(pcm))
            except websockets.ConnectionClosed:
                return

    def _resultado_stt(self, d: dict) -> None:
        final = bool(d.get("is_final"))
        for i, s in enumerate(segmentos(d)):
            if _es_eco(s.texto, self._dichos_agente):
                log.info("Eco del agente descartado del panel: %.60s", s.texto)
                continue
            self.emitir(ev.Transcript(segment_id=f"h-{self._fragmento}-{i}", turn_id=self.maquina.turn_id,
                                      speaker=f"Hablante {s.hablante + 1}", text=s.texto, start=s.inicio,
                                      end=s.fin, is_final=final))
        if final:
            self._fragmento += 1
            if segs := segmentos(d):
                self._ultimo_hablante = f"Hablante {segs[-1].hablante + 1}"

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
            elif mensaje.get("type") == "InjectUserMessage":
                self._textos_pendientes = max(0, self._textos_pendientes - 1)
                if self._historial:  # si la voz nunca abrió, ya se mostró "la voz no está disponible"
                    self.emitir(ev.ErrorEvento(where="voz", message="No pude enviar tu mensaje porque la voz no "
                                               "está conectada; inténtalo de nuevo", recoverable=True))
        self._tarea(esperar_y_enviar(self._listo), "T2-inyectar")

    async def _agente(self) -> None:
        """Mantiene la conexión con Deepgram. Si se corta a mitad de la conversación, reconecta con espera
        creciente y le pasa el historial (agent.context): el usuario sigue donde iba (spec §10.2)."""
        fallos, espera = 0, ESPERA_RECONEXION_S
        while True:
            inicio = asyncio.get_running_loop().time()
            if await self._conectar_agente():
                await self._conversar()
                if asyncio.get_running_loop().time() - inicio > CONEXION_ESTABLE_S:
                    fallos, espera = 0, ESPERA_RECONEXION_S  # venía estable: no cuenta como fallo seguido
                else:
                    fallos += 1
            elif not self._historial:
                return  # nunca hubo conversación: el error ya se mostró; se reintenta con el próximo gesto
            else:
                fallos += 1
            if fallos > MAX_RECONEXIONES:
                self.emitir(ev.ErrorEvento(where="voz", message="No pude reconectar la voz; vuelve a activar el "
                                           "micrófono o escribe para intentarlo de nuevo", recoverable=True))
                return
            log.warning("Sesión %s: conexión con Deepgram cortada; reconecto en %.1fs", self.session_id, espera)
            self.emitir(ev.ErrorEvento(where="voz", message="Se cortó la conexión de voz; reconectando…",
                                       recoverable=True))
            self._listo = asyncio.Event()  # lo que se escriba mientras tanto espera a la reconexión
            await asyncio.sleep(espera)
            espera = min(espera * 2, 8.0)

    async def _conectar_agente(self) -> bool:
        reconexion = bool(self._historial)
        try:
            self.agente = await self._abrir_agente(tools_registry.funciones_agente(self.hub), list(self._historial))
        except ErrorAgente as e:
            log.warning("Sesión %s sin voz: %s", self.session_id, e)
            if not reconexion:
                self.emitir(ev.ErrorEvento(where="voz", message=f"La voz no está disponible: {e}", recoverable=True))
            return False
        except Exception:  # noqa: BLE001 — un bug al preparar la sesión: avisar en vez de quedar mudo
            log.exception("Sesión %s: error abriendo Deepgram", self.session_id)
            self.emitir(ev.ErrorEvento(where="voz", message="La voz no está disponible por un error interno",
                                       recoverable=True))
            return False
        finally:
            self._listo.set()  # lo pendiente se envía o, si no hay agente, se descarta
        log.info("Sesión %s conectada a Deepgram (%s)%s", self.session_id, self.agente.request_id,
                 " con historial" if reconexion else "")
        if reconexion:
            self.emitir(ev.EstadoEvento(state=self.maquina.estado, turn_id=self.maquina.turn_id))
        return True

    async def _conversar(self) -> None:
        """Atiende la conexión abierta hasta que Deepgram la cierre."""
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
        if self.maquina.estado is E.ESCUCHANDO:
            return  # resto del turno interrumpido que aún venía en camino: no se reproduce
        if self.maquina.estado is not E.HABLANDO:
            self._cambio(self.maquina.aplicar(Evento.AGENTE_HABLA))
        if (tr := self._traza_actual()) is not None and tr.primer_audio is None:
            tr.primer_audio = ahora()
        self.emitir(_Audio(self.maquina.turn_id, pcm))

    def _evento_del_agente(self, d: dict) -> None:
        tipo = d.get("type")
        if tipo == "UserStartedSpeaking":
            self._cambio(self.maquina.aplicar(Evento.USUARIO_HABLA))
            if self.maquina.turn_id >= 1:
                self._traza(self.maquina.turn_id)  # empieza a medir el turno
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
            self._lanzar_verificador()
            if (tr := self._traza_actual()) is not None:
                tr.pendientes.discard("audio")
                self._publicar_traza(tr)
        elif tipo == "LatencyReport":
            datos = {k: v for k, v in d.items() if k != "type"}
            self.latencias.update(datos)
            if (tr := self._traza_actual()) is not None and "audio" in tr.pendientes:
                tr.latencias.update(datos)
        elif tipo == "Error":
            log.warning("Deepgram Error en %s: %s", self.session_id, d)
            self.emitir(ev.ErrorEvento(where="voz", message=f"Deepgram: {d.get('description') or d.get('code')}",
                                       recoverable=True))
        elif tipo == "History":
            self._historial.append({k: v for k, v in d.items() if k != "conversational_behavior"})
        elif tipo == "Warning":
            log.info("Deepgram Warning en %s: %s", self.session_id, str(d)[:200])
            if "think provider" in str(d.get("description", "")) and (tr := self._traza_actual()) is not None:
                tr.contexto["llm_respaldo"] = True  # Groq falló: respondió el siguiente proveedor de la cadena

    def _texto(self, rol: str, texto: str) -> None:
        if not texto:
            return
        self._segmentos += 1
        turno = self.maquina.turn_id
        if rol == "user":
            self._cambio(self.maquina.aplicar(Evento.USUARIO_TERMINA))
            if turno >= 1:
                self._traza(turno).pendientes.add("emocion")
            self._tarea(self._emocion(texto, turno), "T5-emocion")  # en paralelo: nunca frena la respuesta
            if self.lecciones is not None and (nombres := correcciones(texto)):
                self._tarea(self._aprender_keyterms(nombres), "lecciones")
            t = self._turno(turno)
            t.pregunta = f"{t.pregunta} {texto}".strip()
            if turno >= 1:
                tr = self._traza(turno)
                tr.fin_usuario = tr.fin_usuario or ahora()
                tr.contexto.update(pregunta=t.pregunta, modelo=self.modelo_llm,
                                   adaptacion_vigente=self.politica.regla.nombre)
            # Por voz, el panel lo llena el STT diarizado; si no está (o fue texto escrito), este texto.
            if self.stt is None or self._textos_pendientes > 0:
                self._textos_pendientes = max(0, self._textos_pendientes - 1)
                self.emitir(ev.Transcript(segment_id=f"u-{self._segmentos}", turn_id=turno, speaker="Hablante 1",
                                          text=texto, is_final=True))
        elif rol == "assistant":
            if self.maquina.estado is E.ESCUCHANDO:
                turno -= 1  # texto del turno interrumpido: se muestra en su turno, no cambia el estado
            self._cambio(self.maquina.aplicar(Evento.AGENTE_HABLA))
            self._dichos_agente.append(texto)
            if (tr := self._trazas.get(turno)) is not None and tr.primer_texto is None:
                tr.primer_texto = ahora()
            if turno not in self._verificados:  # una corrección inyectada no se vuelve a verificar
                t = self._turno(turno)
                t.respuesta = f"{t.respuesta} {texto}".strip()
            self.emitir(ev.AgentText(turn_id=turno, text=texto))
            self.emitir(ev.Transcript(segment_id=f"a-{self._segmentos}", turn_id=turno, speaker="Agente",
                                      text=texto, is_final=True))

    def _cambio(self, cambio: Cambio | None) -> None:
        if cambio is None:
            return
        if cambio.interrumpido:
            if self._verificador is not None:  # el turno quedó a medias: no se verifica ni se corrige
                self._verificador.cancel()
            # Barge-in: la UI vacía su búfer YA; T7 descarta el audio del turno viejo que aún esté en cola.
            self.emitir(ev.EstadoEvento(state=E.INTERRUMPIDO, turn_id=cambio.turn_id - 1))
            self.emitir(ev.AudioFlush(turn_id=cambio.turn_id - 1))
        self.emitir(ev.EstadoEvento(state=cambio.estado, turn_id=cambio.turn_id))

    # ------------------------------- T5 emociones y adaptación -------------------------------

    async def _emocion(self, texto: str, turno: int) -> None:
        tr = self._trazas.get(turno)
        t0 = ahora()
        try:
            await self._emocion_y_adaptacion(texto, turno, tr, t0)
        finally:
            if tr is not None:
                tr.pendientes.discard("emocion")
                self._publicar_traza(tr)

    async def _emocion_y_adaptacion(self, texto: str, turno: int, tr: Traza | None, t0: float) -> None:
        hablante = self._ultimo_hablante
        emo = await self.analizar_emocion(texto)
        if tr is not None:
            tr.etapa("emocion", (ahora() - t0) * 1000, f"{emo.emocion} ({emo.sentimiento:+.2f})")
            tr.contexto["emocion"] = emo.emocion
        self.afecto.agregar(emo)
        self.emitir(ev.Emotion(turn_id=turno, speaker=hablante, sentiment=emo.sentimiento, emotion=emo.emocion,
                               intensity=emo.intensidad, signals=emo.senales))
        cambio = self.politica.evaluar(self.afecto)
        if cambio is None:
            return
        log.info("Sesión %s: adaptación → %s (%s)", self.session_id, cambio.evento.rule, cambio.evento.reason)
        if tr is not None:
            tr.etapa("adaptacion", 0, f"{cambio.evento.rule}: {cambio.evento.reason}"[:200])
        if self.agente is not None:
            for m in cambio.mensajes:
                await self.agente.enviar(m)
        self.emitir(cambio.evento)

    # ------------------------------- traza (#19) -------------------------------

    def _traza(self, n: int) -> Traza:
        if n not in self._trazas:
            self._trazas[n] = Traza(n)
            for viejo in [k for k in self._trazas if k < n - 5]:
                del self._trazas[viejo]
        return self._trazas[n]

    def _traza_actual(self) -> Traza | None:
        return self._trazas.get(self.maquina.turn_id)

    def _publicar_traza(self, tr: Traza) -> None:
        """Emite la traza (la UI reemplaza la anterior del mismo turno) y, cuando ya no falta nada, la guarda."""
        if "audio" in tr.pendientes:
            return  # el turno aún no termina: se publica al terminar el audio
        self.emitir(tr.evento())
        if tr.listo_para_guardar():
            tr.guardar(self.session_id)

    # ------------------------------- T6 verificador -------------------------------

    def _turno(self, n: int) -> verifier.Turno:
        if n not in self._turnos:
            self._turnos[n] = verifier.Turno()
            for viejo in [k for k in self._turnos if k < n - 5]:  # solo los últimos turnos
                del self._turnos[viejo]
        return self._turnos[n]

    def _lanzar_verificador(self) -> None:
        n = self.maquina.turn_id
        t = self._turnos.get(n)
        if n < 1 or t is None or not t.respuesta or n in self._verificados:
            return
        self._verificados.add(n)
        previas = [r for k in sorted(self._turnos) if n - 3 <= k < n for r in self._turnos[k].tools]
        copia = verifier.Turno(t.pregunta, list(t.tools), t.respuesta, previas)
        if (tr := self._trazas.get(n)) is not None:
            tr.pendientes.add("verificador")
        self._verificador = self._tarea(self._verificar(n, copia), "T6-verificador")

    async def _verificar(self, n: int, turno: verifier.Turno) -> None:
        tr = self._trazas.get(n)
        t0 = ahora()
        try:
            v = await self.verificar(turno)
            if tr is not None:
                estado = v.estado if v else "sin veredicto"
                tr.etapa("verificador", (ahora() - t0) * 1000, f"{estado}: {'; '.join(v.problemas)}"[:200]
                         if v and v.problemas else estado)
                tr.contexto["verificacion"] = estado
        finally:
            if tr is not None:
                tr.pendientes.discard("verificador")
                self._publicar_traza(tr)
        if v is None:
            return
        self.emitir(ev.Verification(turn_id=n, status=v.estado, issues=v.problemas, correction=v.correccion))
        if v.estado == "no_respaldado" and v.problemas and self.lecciones is not None:
            await self._aprender_regla(v.problemas[0])
        if v.estado != "no_respaldado" or not v.correccion:
            return
        log.info("Sesión %s: turno %s no respaldado (%s)", self.session_id, n, v.problemas)
        # Solo si nadie habla y seguimos en ese turno: corregir encima de otra conversación confunde más.
        if self.agente is not None and self.maquina.estado is E.INACTIVO and self.maquina.turn_id == n:
            await self.agente.enviar({"type": "InjectAgentMessage", "behavior": "default",
                                      "message": f"Corrijo lo anterior: {v.correccion}"})

    # ------------------------------- lecciones (#23) -------------------------------

    async def _aprender_keyterms(self, nombres: list[str]) -> None:
        """El usuario corrigió un nombre: el STT debe reconocerlo mejor desde ya y en las próximas sesiones."""
        nuevas = [e for n in nombres if (e := await self.lecciones.agregar("keyterm", n, "corrección del usuario"))]
        for e in nuevas:
            self.emitir(e)
        if nuevas and self.agente is not None:
            await self.agente.enviar({"type": "UpdateListen",
                                      "listen": config_listen(KEYTERMS_AGENTE + self.lecciones.keyterms())})

    async def _aprender_regla(self, problema: str) -> None:
        regla = (f"Ya se dijo un dato no respaldado ({problema.rstrip('.')}); di solo cifras y nombres que estén "
                 "en el resultado de la tool.")
        if (e := await self.lecciones.agregar("regla", regla, "verificador")) is not None:
            self.emitir(e)
            if self.agente is not None:
                await self.agente.enviar({"type": "UpdatePrompt", "prompt": f"LECCIÓN APRENDIDA: {regla}"})

    # ------------------------------- T4 tools -------------------------------

    def _pedir_tool(self, f: dict) -> None:
        self._cambio(self.maquina.aplicar(Evento.TOOL_PEDIDA))
        fid = f.get("id") or ""
        tarea = self._tarea(self._ejecutar_tool(fid, f.get("name") or "", f.get("arguments")), f"T4-{f.get('name')}")
        self._tools[fid] = tarea
        # Callback y no `finally`: también corre si la tarea se cancela antes de empezar (request y
        # FunctionCallCancelled en el mismo lote), y así ninguna tool queda "pendiente" para siempre.
        tarea.add_done_callback(lambda t, fid=fid: self._tool_terminada(fid, t))

    def _tool_terminada(self, fid: str, tarea: asyncio.Task) -> None:
        if self._tools.get(fid) is tarea:
            del self._tools[fid]
            if not self._tools:
                self._cambio(self.maquina.aplicar(Evento.TOOL_RESPONDIDA))

    async def _ejecutar_tool(self, fid: str, nombre: str, argumentos) -> None:
        turno = self.maquina.turn_id
        args = _args_para_ui(argumentos)
        self.emitir(ev.ToolCall(turn_id=turno, name=nombre, args=args, status="running"))
        try:
            r = await tools_registry.despachar(self.hub, nombre, argumentos)
            self.emitir(ev.ToolCall(turn_id=turno, name=nombre, args=args, status=r.status, ms=r.ms,
                                    summary=tools_registry.resumen(r.texto)))
            self._turno(turno).tools.append(verifier.ResultadoTool(nombre, args, r.texto))
            if (tr := self._trazas.get(turno)) is not None:
                tr.tool(nombre, r.ms, args, r.texto, r.status + (" (caché)" if r.cache else ""))
            if self.agente is not None:
                await self.agente.enviar({"type": "FunctionCallResponse", "id": fid, "name": nombre,
                                          "content": r.texto})
            # Ya: "pensando" debe verse antes de que Deepgram empiece a hablar con este resultado.
            self._tool_terminada(fid, asyncio.current_task())
        except asyncio.CancelledError:
            # FunctionCallCancelled o barge-in: Deepgram descarta una respuesta tardía; no se envía nada.
            self.emitir(ev.ToolCall(turn_id=turno, name=nombre, args=args, status="cancelled"))


def _poner(cola: asyncio.Queue, pcm: bytes) -> None:
    """Encola audio; si la cola está llena (Deepgram atrasado), descarta lo más viejo."""
    if cola.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            cola.get_nowait()
    cola.put_nowait(pcm)


UMBRAL_ECO = 80


def _es_eco(texto: str, dichos: deque) -> bool:
    """¿Lo que oyó el micrófono es la voz del agente saliendo por los parlantes? El navegador cancela casi
    todo el eco; lo que se cuela se reconoce porque repite lo que el agente acaba de decir."""
    if len(texto) < 12:
        return False
    return any(fuzz.partial_ratio(texto.lower(), d.lower()) >= UMBRAL_ECO for d in dichos)


def _args_para_ui(argumentos) -> dict:
    if isinstance(argumentos, dict):
        return argumentos
    try:
        d = json.loads(argumentos or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}
