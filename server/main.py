"""Servidor del Agente Vocal Cognitivo.  Local:  uv run uvicorn server.main:app --reload

- GET  /             frontend (web/), incluido web/contrato.json
- GET  /api/health   200 si todos los componentes están listos, 503 mientras arrancan
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

app = FastAPI(title="Cognia · Agente Vocal Cognitivo")


@app.get("/api/health")
def health() -> JSONResponse:
    listo = all(componentes.values())
    cuerpo = {"status": "ok" if listo else "iniciando", "componentes": componentes}
    return JSONResponse(cuerpo, status_code=200 if listo else 503)


async def _enviar(ws: WebSocket, evento) -> None:
    await ws.send_text(ev.to_json(evento))


@app.websocket("/ws/voz")
async def ws_voz(ws: WebSocket) -> None:
    await ws.accept()
    session_id = uuid.uuid4().hex[:12]
    await _enviar(ws, ev.Ready(session_id=session_id, voice=config.VOZ, sources=[]))
    turno = 0
    try:
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
    log.info("Sesión %s cerrada", session_id)


# Al final: el montaje en "/" no debe tapar las rutas de arriba.
app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")
