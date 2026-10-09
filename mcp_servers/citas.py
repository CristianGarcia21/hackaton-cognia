"""Servidor MCP de citas: expone las tools de server/tools/citas.py SIN lógica propia (spec §4.2, §8, issue #16).

En proceso (lo usa el hub, #9):   servidor = crear_servidor(HerramientasCitas())
Como proceso aparte (stdio):      uv run python -m mcp_servers.citas
"""

from mcp.server.mcpserver import MCPServer

from mcp_servers.ips import _registrar  # misma firma permisiva + esquema del contrato que el servidor de IPS
from server.tools.citas import ESTADO_PENDIENTE, HerramientasCitas

INSTRUCCIONES = ("Registra y lista SOLICITUDES de cita en sedes de IPS (SQLite). Los datos de IPS no tienen agenda: "
                 f"toda solicitud queda «{ESTADO_PENDIENTE}» y no es una cita confirmada.")


def crear_servidor(herramientas: HerramientasCitas | None = None) -> MCPServer:
    mcp = MCPServer("citas", instructions=INSTRUCCIONES)
    for t in (herramientas or HerramientasCitas()).tools():
        _registrar(mcp, t, t.fn)
    return mcp


if __name__ == "__main__":
    crear_servidor().run()  # stdio
