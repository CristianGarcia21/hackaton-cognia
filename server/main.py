"""Servidor del Agente Vocal Cognitivo.  Local:  uv run uvicorn server.main:app --reload

- GET  /             frontend (web/dist si existe, si no web/), incluido contrato.json
- GET  /api/health   LIVENESS: 200 mientras el proceso responde; el cuerpo dice "ok" o "degradado"
                     (lo usa el healthcheck de Railway: un componente lento no impide el deploy)
- GET  /api/ready    READINESS: 200 solo cuando todos los componentes están listos, si no 503
- WS   /ws/voz       contrato en server/events.py

Esqueleto (issue #2): la sesión real con Deepgram llega en #10; por ahora el WebSocket responde
`ready`, valida los mensajes del cliente y contesta `text_input` con un eco.
"""

import logging
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server import config
from server import events as ev

log = logging.getLogger("cognia.server")

# Componente -> listo. Cada subsistema (dataset, MCP, brief...) se registra aquí al arrancar.
componentes: dict[str, bool] = {}

# Sin /docs ni /openapi.json públicos en la URL del jurado.
app = FastAPI(title="Kognia · Agente Vocal Cognitivo", docs_url=None, redoc_url=None, openapi_url=None)


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
        await _enviar(ws, ev.Ready(session_id=session_id, voice=config.VOZ, sources=[]))
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
