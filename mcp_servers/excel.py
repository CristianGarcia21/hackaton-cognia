"""Servidor MCP de excel: expone las tools de server/tools/excel.py SIN lógica propia (spec §4.2, §8)."""

from mcp.server.mcpserver import MCPServer

from mcp_servers.ips import _registrar  # misma firma permisiva + esquema del contrato que el servidor de IPS
from server.tools.excel import HerramientasExcel


def crear_servidor(herramientas: HerramientasExcel) -> MCPServer:
    mcp = MCPServer("excel")
    for t in herramientas.tools():
        _registrar(mcp, t, t.fn)
    return mcp
