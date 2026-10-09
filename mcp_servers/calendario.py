"""Servidor MCP de calendario: expone las tools de server/tools/calendario.py SIN lógica propia (spec §4.2, §8)."""

from mcp.server.mcpserver import MCPServer

from mcp_servers.ips import _registrar  # misma firma permisiva + esquema del contrato que el servidor de IPS
from server.tools.calendario import HerramientasCalendario


def crear_servidor(herramientas: HerramientasCalendario) -> MCPServer:
    mcp = MCPServer("calendario")
    for t in herramientas.tools():
        _registrar(mcp, t, t.fn)
    return mcp
