"""Servidor del Agente Vocal Cognitivo.  Local:  uv run uvicorn server.main:app --reload

- GET  /             frontend (web/dist si existe, si no web/), incluido contrato.json
- GET  /api/health   LIVENESS: 200 mientras el proceso responde; el cuerpo dice "ok" o "degradado"
                     (lo usa el healthcheck de Railway: un componente lento no impide el deploy)
- GET  /api/ready    READINESS: 200 solo cuando todos los componentes están listos, si no 503
- WS   /ws/voz       contrato en server/events.py

Esqueleto (issue #2): la sesión real con Deepgram llega en #10; por ahora el WebSocket responde
`ready`, valida los mensajes del cliente y contesta `text_input` con un eco.
"""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server import config
from server import events as ev
from server.data import datos_gov

log = logging.getLogger("cognia.server")

# Componente -> listo. Cada subsistema (datos_gov, MCP, brief...) se registra aquí al arrancar.
componentes: dict[str, bool] = {}
REINTENTO_DATOS_S = 15.0  # si datos.gov.co no responde al arrancar, se reintenta cada N segundos


@dataclass
class Estado:
    """Estado compartido del proceso (una sola réplica, spec §14). No guarda el dataset: solo el
    cliente de la API (con su caché) y el catálogo de búsqueda."""

    datos: datos_gov.ClienteDatosGov | None = None
    catalogo: datos_gov.Catalogo | None = None
    fuente: ev.SourceStatus | None = None  # último estado de la fuente: se envía a cada sesión nueva
    conexiones: set[WebSocket] = field(default_factory=set)


estado = Estado()


async def _difundir(evento) -> None:
    """Envía un evento a todas las sesiones abiertas (best-effort: una caída no afecta a las demás)."""
    for ws in list(estado.conexiones):
        with suppress(Exception):
            await _enviar(ws, evento)


async def _conectar_datos_gov() -> None:
    """Carga el catálogo de búsqueda; si la API no responde, reintenta sin tumbar el proceso."""
    componentes["datos_gov"] = False

    async def progreso(s: ev.SourceStatus) -> None:
        estado.fuente = s
        await _difundir(s)

    while estado.catalogo is None:
        try:
            estado.catalogo = await datos_gov.cargar_catalogo(estado.datos, on_status=progreso)
            componentes["datos_gov"] = True
        except Exception as e:  # noqa: BLE001 — /api/ready queda en 503 mientras tanto
            log.warning("datos.gov.co no disponible (%s); reintento en %ss", e, REINTENTO_DATOS_S)
            await asyncio.sleep(REINTENTO_DATOS_S)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    estado.datos = datos_gov.ClienteDatosGov()
    # En segundo plano: /api/health responde mientras se consulta la API.
    tarea = asyncio.create_task(_conectar_datos_gov())
    yield
    tarea.cancel()
    with suppress(asyncio.CancelledError):
        await tarea
    await estado.datos.aclose()


# Sin /docs ni /openapi.json públicos en la URL del jurado.
app = FastAPI(title="Kognia · Agente Vocal Cognitivo", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok" if all(componentes.values()) else "degradado", "componentes": componentes}


@app.get("/api/ready")
def ready() -> JSONResponse:
    listo = all(componentes.values())
    return JSONResponse({"ready": listo, "componentes": componentes}, status_code=200 if listo else 503)


async def _enviar(ws: WebSocket, evento) -> None:
    await ws.send_text(ev.to_json(evento))


@app.websocket("/ws/voz")
async def ws_voz(ws: WebSocket) -> None:
    await ws.accept()
    session_id = uuid.uuid4().hex[:12]
    log.info("Sesión %s abierta", session_id)
    turno = 0
    try:
        await _enviar(ws, ev.Ready(session_id=session_id, voice=config.VOZ, sources=["datos.gov.co"]))
        if estado.fuente is not None:
            await _enviar(ws, estado.fuente)
        estado.conexiones.add(ws)
        while True:
            mensaje = await ws.receive()
            if mensaje["type"] == "websocket.disconnect":
                break
            if mensaje.get("bytes") is not None:
                continue  # audio del micrófono: lo consumirá la sesión de voz (#10)
            recibido = ev.parse_cliente_seguro(mensaje.get("text") or "")
            if isinstance(recibido, ev.ErrorEvento):
                await _enviar(ws, recibido)
            elif isinstance(recibido, ev.TextInput):
                turno += 1
                await _enviar(ws, ev.AgentText(turn_id=turno, text=f"(esqueleto) Recibí: {recibido.text}"))
                await _enviar(ws, ev.EstadoEvento(state=ev.EstadoConversacion.INACTIVO, turn_id=turno))
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — avisar a la UI en vez de cerrar en silencio con 1011
        log.exception("Error en la sesión %s", session_id)
        try:
            await _enviar(ws, ev.ErrorEvento(where="servidor", message="Error interno; reconecta para seguir",
                                             recoverable=False))
            await ws.close(code=1011)
        except Exception:  # noqa: BLE001 — el socket ya puede estar cerrado
            pass
    finally:
        estado.conexiones.discard(ws)
        log.info("Sesión %s cerrada", session_id)


class StaticSinOcultos(StaticFiles):
    """StaticFiles que nunca sirve archivos ni carpetas ocultos (.env, .git...) aunque existan."""

    def lookup_path(self, path: str):
        if any(p.startswith(".") and p != "." for p in path.replace("\\", "/").split("/") if p):
            return "", None
        return super().lookup_path(path)


# Al final: el montaje en "/" no debe tapar las rutas de arriba. Si el frontend llega a tener
# un paso de build, se sirve solo web/dist (nunca el código fuente ni node_modules).
_web = config.WEB_DIR / "dist" if (config.WEB_DIR / "dist").is_dir() else config.WEB_DIR
app.mount("/", StaticSinOcultos(directory=_web, html=True), name="web")
