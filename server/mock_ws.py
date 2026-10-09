"""Servidor de prueba (mock) de /ws/voz para desarrollar la UI sin el backend real (issue #3, spec §6).

    uv run python -m server.mock_ws                    # http://127.0.0.1:8001 · ws://127.0.0.1:8001/ws/voz
    uv run python -m server.mock_ws --auto             # el guion arranca solo al conectar (sin pulsar el micrófono)
    uv run python -m server.mock_ws --velocidad 3      # 3 veces más rápido · --port 8002 · --host 0.0.0.0

Al conectar emite ready → source_status (carga) → brief → state inactivo. Con {"type":"start"} reproduce el
caso María: 4 turnos con 2 hablantes + Agente, tools, emociones, adaptación, verificación, acciones (cita,
calendario, Excel), traza, una interrupción con audio_flush y una lección. {"type":"text_input"} responde un
turno corto. Los mensajes inválidos reciben un `error` recuperable, como en el backend real.

El guion se construye con los modelos de server/events.py AL IMPORTAR este módulo: si el contrato cambia y
el guion ya no lo cumple, el mock falla al arrancar (y tests/test_mock_ws.py también).
Si existe web/, se sirve en la raíz (sin archivos ocultos, como server/main.py) para abrir la UI en la misma URL.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server import events as ev

WEB = Path(__file__).resolve().parent.parent / "web"
COMPONENTES = {"dataset": True, "mcp": True, "brief": True, "mock": True}  # mismo formato que server/main.py
FRAME_MS = 40
TURNOS_MARIA = 4  # turnos del agente en el guion del caso María
ADELANTO_S = 0.2  # el audio TTS se envía ~200 ms antes de que deba sonar, como un TTS en streaming


class StaticSinOcultos(StaticFiles):
    """Copia de server/main.py (no se importa de ahí para que el mock arranque sin las keys ni los servicios
    que main.py inicialice): nunca sirve archivos ni carpetas ocultos (.env, .git...)."""

    def lookup_path(self, path: str):
        if any(p.startswith(".") and p != "." for p in path.replace("\\", "/").split("/") if p):
            return "", None
        return super().lookup_path(path)


@dataclass
class Pausa:
    """Espera `s` segundos (escalados por --velocidad) antes del siguiente paso."""

    s: float


@dataclass
class Audio:
    """Envía `s` segundos de audio TTS falso (frames binarios PCM16 24 kHz) a ritmo de tiempo real."""

    s: float


Paso = BaseModel | Pausa | Audio


def _tono(segundos: float) -> list[bytes]:
    """Tono suave de 440 Hz con fundido, partido en frames de FRAME_MS (formato AUDIO_SALIDA)."""
    rate = ev.AUDIO_SALIDA["sample_rate"]
    por_frame = rate * FRAME_MS // 1000
    t = np.arange(max(1, round(segundos * 1000 / FRAME_MS)) * por_frame) / rate  # frames completos
    fundido = np.minimum(1, np.minimum(t, t[-1] - t) / 0.05)
    pcm = (0.12 * 32767 * np.sin(2 * np.pi * 440 * t) * fundido).astype("<i2").tobytes()
    paso = por_frame * 2  # bytes por frame (2 bytes por muestra)
    return [pcm[i:i + paso] for i in range(0, len(pcm), paso)]


def _parciales(segment_id: str, speaker: str, texto: str, start: float, end: float) -> list[Paso]:
    """Transcripción en vivo: parciales que crecen y el final, con el mismo segment_id."""
    palabras = texto.split()
    cortes = sorted({max(1, len(palabras) // 3), max(1, 2 * len(palabras) // 3)})
    pasos: list[Paso] = []
    for n in cortes:
        if n < len(palabras):
            pasos += [ev.Transcript(segment_id=segment_id, speaker=speaker,
                                    text=" ".join(palabras[:n]), start=start, is_final=False), Pausa(0.35)]
    pasos.append(ev.Transcript(segment_id=segment_id, speaker=speaker, text=texto,
                               start=start, end=end, is_final=True))
    return pasos


def _herramienta(turn_id: int, name: str, args: dict, ms: int, summary: str) -> list[Paso]:
    return [
        ev.EstadoEvento(state=ev.EstadoConversacion.EJECUTANDO_TOOL, turn_id=turn_id),
        ev.ToolCall(turn_id=turn_id, name=name, args=args, status="running"),
        Pausa(ms / 1000 + 0.2),
        ev.ToolCall(turn_id=turn_id, name=name, args=args, status="ok", ms=ms, summary=summary),
        ev.EstadoEvento(state=ev.EstadoConversacion.PENSANDO, turn_id=turn_id),
    ]


def _respuesta(turn_id: int, texto: str, audio_s: float, durante: tuple[Paso, ...] = ()) -> list[Paso]:
    """El agente habla; `durante` son eventos en paralelo (emoción, adaptación) que llegan mientras suena."""
    return [
        ev.EstadoEvento(state=ev.EstadoConversacion.HABLANDO, turn_id=turn_id),
        ev.AgentText(turn_id=turn_id, text=texto),
        ev.Transcript(segment_id=f"a-{turn_id}", turn_id=turn_id, speaker="Agente", text=texto, is_final=True),
        Audio(audio_s / 2), *durante, Audio(audio_s / 2),
    ]


def conexion() -> list[Paso]:
    """Lo que ve la UI al abrir la URL: sesión lista, carga del dataset (P2) y brief (P3)."""
    total, por_pagina = 41427, 1000  # como el loader real (spec §7.2)
    pasos: list[Paso] = [
        ev.Ready(session_id="mock", voice="aura-2-celeste-es", sources=["ips", "citas", "excel", "calendario"]),
        ev.SourceStatus(source="datos.gov.co", status="conectando"),
        Pausa(0.4),
    ]
    for pagina in range(1, -(-total // por_pagina) + 1):
        filas = min(pagina * por_pagina, total)
        pasos += [ev.SourceStatus(source="datos.gov.co", status="descargando", rows=filas, total_rows=total,
                                  pages=pagina, progress=filas / total), Pausa(0.06)]
    pasos += [
        ev.SourceStatus(source="datos.gov.co", status="listo", rows=total, total_rows=total,
                        pages=-(-total // por_pagina), progress=1),
        Pausa(0.3),
        ev.Brief(
            summary="Registro de 10 921 sedes de IPS de Colombia con su capacidad instalada (corte nov. 2022).",
            key_points=["41 427 registros de capacidad", "Antioquia y Bogotá concentran la mayor oferta",
                        "Nivel de atención sin dato en el 61 %"],
            questions=["¿Cuántas camas de UCI hay en Antioquia?", "¿Qué IPS públicas tienen quirófano en Cali?",
                       "¿Cómo se distribuyen las ambulancias por departamento?",
                       "Necesito una cita para una cirugía en Medellín"],
            stats={"filas": total, "sedes": 10921, "prestadores": 9320},
        ),
        ev.EstadoEvento(state=ev.EstadoConversacion.INACTIVO),
    ]
    return pasos


def guion_maria(base: int = 0) -> list[Paso]:
    """Caso María (spec §1): búsqueda → adaptación a la ansiedad → cita + calendario + Excel →
    interrupción → pregunta fuera de los datos → lección. Usa los turn_id base+1 … base+TURNOS_MARIA."""
    E = ev.EstadoConversacion
    CORTADO = "Además del Pablo Tobón Uribe están la Clínica Las Américas, la Clínica Medellín, el Hospital General y…"
    t1, t2, t3, t4 = (base + i for i in range(1, TURNOS_MARIA + 1))
    ips_args = {"municipio": "Medellín", "capacidad": "Sala de Cirugía"}
    cita = {"id": 7, "paciente": "María Gómez", "sede": "Hospital Pablo Tobón Uribe",
            "motivo": "valoración para cirugía", "fecha_preferida": "2026-10-13 08:00",
            "estado": "pendiente de confirmación por la IPS"}
    return [
        # ---- Turno 1 · María pide la cita (ansiedad) → búsqueda en los datos ----
        ev.EstadoEvento(state=E.ESCUCHANDO),
        *_parciales(f"h-{t1}", "Hablante 1", "Necesito una cita para una cirugía en Medellín, estoy muy preocupada",
                    3.2, 6.9),
        ev.EstadoEvento(state=E.PENSANDO, turn_id=t1),
        Pausa(0.3),
        *_herramienta(t1, "buscar_ips", ips_args, 12, "5 sedes encontradas"),
        # La emoción se analiza en paralelo (nunca bloquea la voz) y la adaptación aplica desde el turno 2.
        *_respuesta(t1, "Encontré cinco sedes en Medellín con sala de cirugía. La más cercana al centro es el "
                       "Hospital Pablo Tobón Uribe. ¿Quiere que le registre la solicitud de cita allí?", 2.5,
                    durante=(ev.Emotion(turn_id=t1, speaker="Hablante 1", sentiment=-0.35, emotion="ansiedad",
                                        intensity=0.65, signals=["cirugía", "muy preocupada"]),
                             ev.Adaptation(rule="ansiedad", style="Cálido y tranquilizador", speed=0.9,
                                           reason="ansiedad detectada (sentimiento -0.35)"))),
        ev.EstadoEvento(state=E.INACTIVO),
        ev.Verification(turn_id=t1, status="respaldado"),
        ev.Trace(turn_id=t1, spans=[ev.Span(stage="fin_turno", ms=380), ev.Span(stage="llm_decide", ms=190),
                                   ev.Span(stage="tool:buscar_ips", ms=12, detail="municipio=Medellín · 5 filas"),
                                   ev.Span(stage="llm_redacta", ms=210), ev.Span(stage="primer_audio", ms=240),
                                   ev.Span(stage="emocion", ms=420), ev.Span(stage="verificador", ms=610)],
                 context={"adaptacion": "normal", "lecciones": 0, "tools": ["buscar_ips"]}),
        Pausa(1.2),

        # ---- Turno 2 · el acompañante (Hablante 2) confirma → cita + calendario + Excel ----
        ev.EstadoEvento(state=E.ESCUCHANDO),
        *_parciales(f"h-{t2}", "Hablante 2", "Sí, regístrela a nombre de María Gómez para el lunes en la mañana",
                    14.8, 18.1),
        ev.EstadoEvento(state=E.PENSANDO, turn_id=t2),
        Pausa(0.3),
        *_herramienta(t2, "registrar_solicitud_cita",
                      {"paciente": "María Gómez", "sede_codigo": "0500102345", "motivo": "valoración para cirugía",
                       "fecha_preferida": "2026-10-13 08:00"}, 35, "solicitud 7 registrada"),
        ev.Action(kind="cita", data=cita),
        *_herramienta(t2, "crear_evento_cita", {"solicitud_id": 7, "fecha_hora": "2026-10-13T08:00",
                                               "duracion_min": 30}, 28, "evento creado"),
        ev.Action(kind="evento", data={"solicitud_id": 7, "titulo": "Solicitud de cita (pendiente de confirmación "
                                       "por la IPS)", "inicio": "2026-10-13T08:00", "duracion_min": 30},
                  link="/api/calendario.ics"),
        *_herramienta(t2, "exportar_solicitudes_excel", {}, 41, "1 fila exportada"),
        ev.Action(kind="excel", data={"filas": 1}, link="/api/citas/excel"),
        *_respuesta(t2, "Tranquilos, ya quedó. Registré la solicitud de María para el lunes a las 8 de la "
                        "mañana en el Hospital Pablo Tobón Uribe. Ojo: queda pendiente de confirmación por la IPS. "
                        "También la dejé en el calendario y en Excel.", 2.5,
                    durante=(ev.Emotion(turn_id=t2, speaker="Hablante 2", sentiment=0.2, emotion="calma",
                                        intensity=0.4, signals=["regístrela"]),)),
        ev.EstadoEvento(state=E.INACTIVO),
        ev.Verification(turn_id=t2, status="respaldado"),
        ev.Trace(turn_id=t2, spans=[ev.Span(stage="fin_turno", ms=350), ev.Span(stage="llm_decide", ms=220),
                                   ev.Span(stage="tool:registrar_solicitud_cita", ms=35),
                                   ev.Span(stage="tool:crear_evento_cita", ms=28),
                                   ev.Span(stage="tool:exportar_solicitudes_excel", ms=41),
                                   ev.Span(stage="llm_redacta", ms=240), ev.Span(stage="primer_audio", ms=260)],
                 context={"adaptacion": "ansiedad", "lecciones": 0,
                          "tools": ["registrar_solicitud_cita", "crear_evento_cita", "exportar_solicitudes_excel"]}),
        Pausa(1.2),

        # ---- Turno 3 · respuesta larga que María interrumpe → audio_flush ----
        ev.EstadoEvento(state=E.ESCUCHANDO),
        *_parciales(f"h-{t3}", "Hablante 1", "¿Y qué otras clínicas tienen cirugía?", 25.0, 26.8),
        ev.EstadoEvento(state=E.PENSANDO, turn_id=t3),
        *_herramienta(t3, "buscar_ips", {**ips_args, "limite": 5}, 9, "5 sedes encontradas (caché)"),
        ev.EstadoEvento(state=E.HABLANDO, turn_id=t3),
        ev.AgentText(turn_id=t3, text=CORTADO),
        ev.Transcript(segment_id=f"a-{t3}", turn_id=t3, speaker="Agente", text=CORTADO, is_final=True),
        Audio(1.2),
        ev.EstadoEvento(state=E.INTERRUMPIDO, turn_id=t3),
        ev.AudioFlush(turn_id=t3),
        ev.Trace(turn_id=t3, spans=[ev.Span(stage="fin_turno", ms=360), ev.Span(stage="llm_decide", ms=180),
                                   ev.Span(stage="tool:buscar_ips", ms=9, detail="caché"),
                                   ev.Span(stage="llm_redacta", ms=200), ev.Span(stage="primer_audio", ms=230)],
                 context={"adaptacion": "ansiedad", "lecciones": 0, "interrumpido": True,
                          "tools": ["buscar_ips"]}),

        # ---- Turno 4 · pregunta fuera de los datos → honestidad ----
        ev.EstadoEvento(state=E.ESCUCHANDO),
        *_parciales(f"h-{t4}", "Hablante 1", "Espere, ¿cuánto se demoran en atenderme allá?", 28.1, 30.4),
        ev.EstadoEvento(state=E.PENSANDO, turn_id=t4),
        Pausa(0.4),
        *_respuesta(t4, "Ese dato no está en la información de las IPS que tengo: no incluye tiempos de espera. "
                       "Le recomiendo preguntarlo cuando la IPS la llame para confirmar la cita.", 2.0),
        ev.EstadoEvento(state=E.INACTIVO),
        ev.Verification(turn_id=t4, status="fuera_de_datos_ok"),
        ev.Trace(turn_id=t4, spans=[ev.Span(stage="fin_turno", ms=370), ev.Span(stage="llm_decide", ms=200),
                                   ev.Span(stage="llm_redacta", ms=190), ev.Span(stage="primer_audio", ms=250)],
                 context={"adaptacion": "ansiedad", "lecciones": 0, "tools": []}),
        ev.Lesson(kind="keyterm", content="Pablo Tobón Uribe", origin="«¿quisiste decir…?» confirmado"),
    ]


def turno_texto(turn_id: int, texto: str) -> list[Paso]:
    """Respuesta corta a un text_input (respaldo por texto o pregunta pulsada del brief)."""
    E = ev.EstadoConversacion
    return [
        ev.Transcript(segment_id=f"t-{turn_id}", turn_id=turn_id, speaker="Hablante 1", text=texto, is_final=True),
        ev.EstadoEvento(state=E.PENSANDO, turn_id=turn_id),
        *_herramienta(turn_id, "describir_datos", {}, 5, "esquema y totales"),
        *_respuesta(turn_id, f"(Mock) Recibí: «{texto}». Con el backend real, aquí responde el agente.", 1.0),
        ev.EstadoEvento(state=E.INACTIVO),
        ev.Verification(turn_id=turn_id, status="fuera_de_datos_ok"),
        ev.Trace(turn_id=turn_id, spans=[ev.Span(stage="llm_decide", ms=150), ev.Span(stage="tool:describir_datos",
                                                                                      ms=5)],
                 context={"modo": "texto", "tools": ["describir_datos"]}),
    ]


# Se construyen al importar: si el contrato cambia y el guion ya no lo cumple, falla aquí.
CONEXION = conexion()
GUION_MARIA = guion_maria()


def create_app(velocidad: float = 1.0, auto: bool = False) -> FastAPI:
    """velocidad: divide las pausas y el ritmo del audio (0 = sin esperas, para los tests)."""
    app = FastAPI(title="Kognia · mock de /ws/voz")

    async def esperar(s: float) -> None:
        if velocidad > 0:
            await asyncio.sleep(s / velocidad)

    async def enviar_audio(cola: asyncio.Queue, segundos: float) -> None:
        """Frames a ritmo de tiempo real medido contra el reloj (en Windows asyncio.sleep se pasa ~15 ms y
        acumularía retraso) y con ~200 ms de adelanto, como un TTS real: la UI no se queda sin datos."""
        frames = _tono(segundos)
        if velocidad <= 0:
            for frame in frames:
                await cola.put(frame)
            return
        loop = asyncio.get_running_loop()
        inicio, dt = loop.time(), FRAME_MS / 1000 / velocidad
        for i, frame in enumerate(frames):
            await cola.put(frame)
            espera = inicio + (i + 1) * dt - ADELANTO_S - loop.time()
            if espera > 0:
                await asyncio.sleep(espera)
        # Como un TTS real, el resto del guion sigue cuando se ENVIÓ el audio (no cuando terminó de sonar): se conserva
        # el adelanto de ADELANTO_S y la UI no se queda sin datos entre dos trozos seguidos de la misma respuesta.
        await asyncio.sleep(max(0, inicio + len(frames) * dt - ADELANTO_S - loop.time()))

    @app.get("/api/brief")
    async def brief() -> dict:
        return next(p for p in CONEXION if isinstance(p, ev.Brief)).model_dump(mode="json")

    @app.get("/api/calendario.ics")
    async def calendario() -> Response:
        ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Kognia//mock//ES\r\nBEGIN:VEVENT\r\n"
               "UID:mock-7@kognia\r\nDTSTART:20261013T080000\r\nDURATION:PT30M\r\n"
               "SUMMARY:Solicitud de cita (pendiente de confirmación por la IPS)\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        return Response(ics, media_type="text/calendar")

    @app.get("/api/citas/excel")
    async def excel() -> Response:
        return Response("En el mock no hay Excel: lo genera el MCP excel del backend real.", status_code=501)

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "componentes": COMPONENTES}

    @app.get("/api/ready")
    async def ready() -> dict:
        return {"ready": True, "componentes": COMPONENTES}

    @app.websocket("/ws/voz")
    async def ws_voz(ws: WebSocket) -> None:
        await ws.accept()
        cola: asyncio.Queue[str | bytes] = asyncio.Queue()
        siguiente_turno = 1  # como el backend real: empieza en 1 y solo crece
        en_curso: asyncio.Task | None = None

        async def emisor() -> None:  # T7: única tarea que escribe en el WebSocket
            while True:
                item = await cola.get()
                await (ws.send_bytes(item) if isinstance(item, bytes) else ws.send_text(item))

        async def reproducir(pasos: list[Paso]) -> None:
            for paso in pasos:
                if isinstance(paso, Pausa):
                    await esperar(paso.s)
                elif isinstance(paso, Audio):
                    await enviar_audio(cola, paso.s)
                else:
                    await cola.put(ev.to_json(paso))

        def lanzar(pasos: list[Paso]) -> bool:
            nonlocal en_curso
            if en_curso and not en_curso.done():
                return False
            en_curso = asyncio.create_task(reproducir(pasos))
            return True

        def lanzar_guion() -> bool:
            nonlocal siguiente_turno
            if not lanzar(guion_maria(siguiente_turno - 1)):
                return False
            siguiente_turno += TURNOS_MARIA
            return True

        tarea_emisor = asyncio.create_task(emisor())
        guion_reproducido = False
        try:
            await reproducir(CONEXION)
            if auto:
                guion_reproducido = lanzar_guion()
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    continue  # audio del micrófono: el mock lo ignora
                cliente = ev.parse_cliente_seguro(msg.get("text") or "")
                if isinstance(cliente, ev.ErrorEvento):
                    await cola.put(ev.to_json(cliente))
                elif isinstance(cliente, ev.Start) and not guion_reproducido:
                    guion_reproducido = lanzar_guion()
                # stop solo apaga el micrófono (no cancela al agente), así que el mock lo ignora.
                elif isinstance(cliente, ev.TextInput):
                    if lanzar(turno_texto(siguiente_turno, cliente.text)):
                        siguiente_turno += 1
                    else:
                        await cola.put(ev.to_json(ev.ErrorEvento(
                            where="mock", message="El guion sigue en curso; espera a que termine.",
                            recoverable=True)))
        except WebSocketDisconnect:
            pass
        finally:
            for tarea in (en_curso, tarea_emisor):
                if tarea:
                    tarea.cancel()

    if WEB.is_dir():  # al final: las rutas de arriba tienen prioridad sobre los archivos estáticos
        app.mount("/", StaticSinOcultos(directory=WEB, html=True), name="web")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock del WebSocket /ws/voz (caso María)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--velocidad", type=float, default=1.0, help="multiplica la rapidez del guion")
    parser.add_argument("--auto", action="store_true", help="reproduce el guion al conectar, sin esperar start")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # la consola de Windows no imprime "·" sin esto
    print(f"Mock listo: http://{args.host}:{args.port}  ·  ws://{args.host}:{args.port}/ws/voz")
    uvicorn.run(create_app(args.velocidad, args.auto), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
