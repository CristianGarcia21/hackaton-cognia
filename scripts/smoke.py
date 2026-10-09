"""Smoke test de un despliegue:  uv run python scripts/smoke.py https://<tu-app>.up.railway.app

Comprueba lo que el jurado verá primero (paso P1): la página carga, el servidor está vivo,
no expone secretos ni docs, y el WebSocket responde `ready` y procesa un `text_input`.
Sale con código 1 si algo falla.
"""

import asyncio
import sys

import httpx
import websockets

from server import events as ev


async def main(base: str) -> bool:
    base = base.rstrip("/")
    ok = True

    def check(nombre: str, condicion: bool, detalle: str = "") -> None:
        nonlocal ok
        ok &= condicion
        print(f"{'✅' if condicion else '❌'} {nombre}{' · ' + detalle if detalle else ''}")

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
        r = await http.get(f"{base}/")
        check("GET / carga la página", r.status_code == 200 and "text/html" in r.headers.get("content-type", ""))
        r = await http.get(f"{base}/api/health")
        check("GET /api/health", r.status_code == 200, r.text[:120])
        r = await http.get(f"{base}/api/ready")
        check("GET /api/ready", r.status_code == 200, r.text[:120])
        for ruta in ("/.env", "/docs", "/openapi.json"):
            r = await http.get(f"{base}{ruta}")
            check(f"no expone {ruta}", r.status_code == 404, str(r.status_code))

    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/ws/voz"
    try:
        async with websockets.connect(ws_url, open_timeout=20) as ws:
            listo = ev.parse_servidor(await asyncio.wait_for(ws.recv(), 20))
            check("WS /ws/voz responde ready", isinstance(listo, ev.Ready), getattr(listo, "session_id", ""))
            await ws.send(ev.to_json(ev.TextInput(text="prueba de humo")))
            respuesta = ev.parse_servidor(await asyncio.wait_for(ws.recv(), 30))
            check("WS procesa text_input", not isinstance(respuesta, ev.ErrorEvento), respuesta.type)
    except Exception as e:  # noqa: BLE001
        check("WS /ws/voz", False, f"{type(e).__name__}: {e}")
    return ok


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(0 if asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000")) else 1)
