"""Servidor del Agente Vocal Cognitivo.  Local:  uv run uvicorn server.main:app --reload

- GET  /             frontend (web/dist si existe, si no web/), incluido contrato.json
- GET  /api/health   LIVENESS: 200 mientras el proceso responde; el cuerpo dice "ok" o "degradado"
                     (lo usa el healthcheck de Railway: un componente lento no impide el deploy)
- GET  /api/ready    READINESS: 200 solo cuando todos los componentes están listos, si no 503
- GET  /api/brief    brief de la fuente (spec §9.5); 503 mientras se calcula
- WS   /ws/voz       contrato en server/events.py

Al arrancar: catálogo de datos.gov.co (en segundo plano) y hub MCP con las tools (#9). Cada WebSocket es
una Sesion (server/session.py, #10) conectada al Deepgram Voice Agent con las tools del hub.
"""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server import config
from server import events as ev
from server import tools_registry
from server.cognition import brief as brief_fuente
from server.data import datos_gov
from server.deepgram_agent import ConexionAgente, settings
from server.deepgram_stt import ConexionSTT
from server.mcp_hub import HubMCP
from server.session import Sesion
from server.tools.ips import HerramientasIPS

log = logging.getLogger("cognia.server")

# Componente -> listo. Cada subsistema (datos_gov, MCP, brief...) se registra aquí al arrancar.
componentes: dict[str, bool] = {}
REINTENTO_DATOS_S = 15.0  # si datos.gov.co no responde al arrancar, se reintenta cada N segundos
REINTENTO_BRIEF_S = 30.0


@dataclass
class Estado:
    """Estado compartido del proceso (una sola réplica, spec §14). No guarda el dataset: solo el
    cliente de la API (con su caché) y el catálogo de búsqueda."""

    datos: datos_gov.ClienteDatosGov | None = None
    catalogo: datos_gov.Catalogo | None = None
    fuente: ev.SourceStatus | None = None  # último estado de la fuente: se envía a cada sesión nueva
    ips: HerramientasIPS | None = None  # tools de IPS: reciben el catálogo cuando termina de cargar
    hub: HubMCP | None = None  # servidores MCP; sus tools van al Voice Agent (server/tools_registry.py)
    conexiones: set[Sesion] = field(default_factory=set)  # sesiones abiertas (para difundir eventos)
    brief: ev.Brief | None = None  # caché: se calcula una vez al arrancar y se envía a cada sesión (#12)
    saludo: str | None = None  # versión hablada del brief para el greeting del Voice Agent


estado = Estado()


async def _difundir(evento) -> None:
    """Encola un evento en todas las sesiones abiertas. No espera a ningún navegador: cada sesión lo
    escribe con su propio emisor (T7), así que una sesión lenta no frena a las demás."""
    for sesion in list(estado.conexiones):
        sesion.emitir(evento)


async def _conectar_datos_gov() -> None:
    """Carga el catálogo de búsqueda; si la API no responde, reintenta sin tumbar el proceso."""
    componentes["datos_gov"] = False

    async def progreso(s: ev.SourceStatus) -> None:
        estado.fuente = s
        await _difundir(s)

    while estado.catalogo is None:
        try:
            estado.catalogo = await datos_gov.cargar_catalogo(estado.datos, on_status=progreso)
            if estado.ips is not None:
                estado.ips.catalogo = estado.catalogo
            componentes["datos_gov"] = True
            await _preparar_brief()
        except datos_gov.FuenteNoDisponible as e:  # caída de la API: /api/ready queda en 503 y se reintenta
            log.warning("datos.gov.co no disponible (%s); reintento en %ss", e, REINTENTO_DATOS_S)
            await asyncio.sleep(REINTENTO_DATOS_S)
        except Exception:  # noqa: BLE001 — un bug (consulta inválida, respuesta inesperada): no reintentar
            log.exception("Error inesperado cargando el catálogo de datos.gov.co; no se reintenta")
            return


async def _preparar_brief() -> None:
    """Brief con estadísticas reales (+ LLM o respaldo). Si datos.gov.co falla, reintenta sin tumbar nada."""
    componentes["brief"] = False
    while estado.brief is None:
        try:
            r = await brief_fuente.generar(estado.datos, estado.catalogo)
        except datos_gov.FuenteNoDisponible as e:
            log.warning("Brief: datos.gov.co no disponible (%s); reintento en %ss", e, REINTENTO_BRIEF_S)
            await asyncio.sleep(REINTENTO_BRIEF_S)
            continue
        except Exception:  # noqa: BLE001 — un bug no debe tumbar el arranque: sin brief la voz sigue
            log.exception("Error inesperado generando el brief; se omite")
            return
        estado.brief, estado.saludo = r.brief, r.saludo
        componentes["brief"] = True
        log.info("Brief listo (%s)", "LLM" if r.con_llm else "respaldo sin LLM")
        await _difundir(r.brief)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    estado.datos = datos_gov.ClienteDatosGov()
    # En segundo plano: /api/health responde mientras se consulta la API.
    tarea = asyncio.create_task(_conectar_datos_gov())
    # Hub MCP: las tools de IPS existen desde ya y responden "cargando" hasta que llega el catálogo.
    try:
        estado.ips = HerramientasIPS(estado.datos, estado.catalogo)
        estado.hub = HubMCP()
        tools_registry.registrar_servidores(estado.hub, estado.ips)
        await estado.hub.iniciar()
        yield
    finally:
        if estado.hub is not None:
            await estado.hub.cerrar()
        tarea.cancel()
        with suppress(asyncio.CancelledError):
            await tarea
        await estado.datos.aclose()


# Sin /docs ni /openapi.json públicos en la URL del jurado.
app = FastAPI(title="Kognia · Agente Vocal Cognitivo", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


def _componentes() -> dict[str, bool]:
    if estado.hub is not None:  # el hub cambia solo (reconexiones): se lee en cada consulta
        componentes["mcp"] = estado.hub.listo()
    return componentes


@app.get("/api/health")
def health() -> dict:
    c = _componentes()
    return {"status": "ok" if all(c.values()) else "degradado", "componentes": c}


@app.get("/api/brief")
def brief() -> JSONResponse:
    if estado.brief is None:
        return JSONResponse({"ready": False, "detail": "El brief se está calculando"}, status_code=503)
    return JSONResponse(estado.brief.model_dump(mode="json"))


@app.get("/api/ready")
def ready() -> JSONResponse:
    c = _componentes()
    listo = all(c.values())
    return JSONResponse({"ready": listo, "componentes": c}, status_code=200 if listo else 503)


async def _enviar(ws: WebSocket, evento) -> None:
    await ws.send_text(ev.to_json(evento))


async def _abrir_agente(funciones: list[dict], historial: list[dict] | None = None) -> ConexionAgente:
    extra = {"saludo": estado.saludo} if estado.saludo else {}  # saludo del brief (#12); sin brief, el de por defecto
    return await ConexionAgente.abrir(config.DEEPGRAM_API_KEY, settings(
        funciones, groq_key=config.GROQ_API_KEY, groq_key_2=config.GROQ_API_KEY_2, modelo=config.VOICE_LLM,
        voz=config.VOZ, historial=historial, **extra))


async def _abrir_stt() -> ConexionSTT:
    return await ConexionSTT.abrir(config.DEEPGRAM_API_KEY)


@app.websocket("/ws/voz")
async def ws_voz(ws: WebSocket) -> None:
    await ws.accept()
    session_id = uuid.uuid4().hex[:12]
    log.info("Sesión %s abierta", session_id)
    sesion = Sesion(ws, session_id, estado.hub, _abrir_agente, _abrir_stt, voz=config.VOZ)
    sesion.modelo_llm = config.VOICE_LLM
    iniciales = [ev.Ready(session_id=session_id, voice=config.VOZ, sources=["datos.gov.co"])]
    if estado.fuente is not None:
        iniciales.append(estado.fuente)
    if estado.brief is not None:
        iniciales.append(estado.brief)
    estado.conexiones.add(sesion)  # antes de correr: así no se pierde un "listo" que llegue ahora
    try:
        await sesion.correr(iniciales)
    except Exception:  # noqa: BLE001 — avisar a la UI en vez de cerrar en silencio con 1011
        log.exception("Error en la sesión %s", session_id)
        try:
            await _enviar(ws, ev.ErrorEvento(where="servidor", message="Error interno; reconecta para seguir",
                                             recoverable=False))
            await ws.close(code=1011)
        except Exception:  # noqa: BLE001 — el socket ya puede estar cerrado
            pass
    finally:
        estado.conexiones.discard(sesion)
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
