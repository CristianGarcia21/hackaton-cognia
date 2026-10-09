"""Registro de tools para el Voice Agent de Deepgram (spec §8): qué servidores MCP hay, cómo se declaran
sus tools en `agent.think.functions` y cómo se despacha un FunctionCallRequest.

Las funciones se declaran SIN `endpoint`: Deepgram las pide al cliente (este backend) con
FunctionCallRequest y nosotros respondemos con FunctionCallResponse (lo hace la sesión, #10).

Para agregar un servidor MCP (citas, excel, calendario): una línea en `registrar_servidores`.
"""

import json

from mcp_servers import ips as mcp_ips
from server.mcp_hub import HubMCP, Resultado
from server.tools.ips import HerramientasIPS

MAX_RESUMEN = 90
TIMEOUT_IPS_S = 5.0


def registrar_servidores(hub: HubMCP, ips: HerramientasIPS) -> None:
    """Todos los servidores MCP del agente. `cacheable=True` solo para lecturas sin efectos."""
    # IPS: 5 s porque ClienteDatosGov ya acota cada consulta a 4.5 s (reintento incluido) y responde con
    # un mensaje honesto ("datos.gov.co no responde"); con 3 s el hub cortaría antes de ese mensaje.
    hub.registrar("ips", mcp_ips.crear_servidor(ips), cacheable=True, timeout_s=TIMEOUT_IPS_S)
    # hub.registrar("citas", mcp_citas.crear_servidor(...))            # #16 (Carlos)
    # hub.registrar("excel", mcp_excel.crear_servidor(...))            # #17 (Carlos)
    # hub.registrar("calendario", mcp_calendario.crear_servidor(...))  # #18 (Carlos)


def funciones_agente(hub: HubMCP) -> list[dict]:
    """`agent.think.functions` para el mensaje Settings del Voice Agent."""
    return [{"name": t.nombre, "description": t.descripcion, "parameters": _limpiar(t.esquema)}
            for t in hub.tools()]


def _limpiar(esquema):
    """Quita los `title` que agrega Pydantic: no le aportan nada al LLM y cuestan tokens en cada turno."""
    if isinstance(esquema, dict):
        return {k: _limpiar(v) for k, v in esquema.items() if k != "title"}
    if isinstance(esquema, list):
        return [_limpiar(v) for v in esquema]
    return esquema


async def despachar(hub: HubMCP, nombre: str, argumentos: str | dict | None) -> Resultado:
    """Ejecuta una FunctionCallRequest. Deepgram envía los argumentos como texto JSON."""
    if isinstance(argumentos, str):
        try:
            argumentos = json.loads(argumentos) if argumentos.strip() else {}
        except json.JSONDecodeError:
            argumentos = []  # cae en el error de abajo
    if argumentos is None:
        argumentos = {}
    if not isinstance(argumentos, dict):
        return Resultado("Error: los argumentos deben ser un objeto JSON con los parámetros de la tool.",
                         "error", 0.0)
    return await hub.llamar(nombre, argumentos)


def resumen(texto: str) -> str:
    """Primera línea, recortada: el `summary` del evento `tool` del Inspector."""
    linea = (texto or "").strip().split("\n", 1)[0]
    return linea if len(linea) <= MAX_RESUMEN else linea[:MAX_RESUMEN - 1] + "…"
