"""Servidor MCP de IPS: expone las tools de server/tools/ips.py SIN lógica propia (spec §4.2, §8).

En proceso (lo usa el hub, #9):   servidor = crear_servidor(HerramientasIPS(cliente, catalogo))
Como proceso aparte (stdio, p. ej. Claude Desktop o un hub en otra máquina):
    uv run python -m mcp_servers.ips
"""

import inspect
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from server.data import datos_gov
from server.tools.ips import HerramientasIPS

INSTRUCCIONES = ("Consulta en vivo la relación de IPS públicas y privadas de Colombia con su capacidad "
                 "instalada (datos.gov.co, REPS, corte nov. 2022). No incluye horarios ni disponibilidad.")


def crear_servidor(herramientas: HerramientasIPS) -> MCPServer:
    """Servidor MCP con las tools de un HerramientasIPS ya inicializado (modo en proceso: lo usa el hub)."""
    mcp = MCPServer("ips", instructions=INSTRUCCIONES)
    for t in herramientas.tools():
        mcp.add_tool(t.fn, name=t.name, description=t.description)
        _copiar_descripciones(mcp, t)
    return mcp


def _copiar_descripciones(mcp: MCPServer, t) -> None:
    """El SDK arma el inputSchema desde la firma y pierde las descripciones de `Args:` del docstring.
    Se copian desde el contrato Tool para que el LLM vea lo mismo por MCP que por el contrato."""
    registrada = mcp._tool_manager.get_tool(t.name)
    props = (registrada.parameters or {}).get("properties", {})
    for nombre, esquema in t.parameters.get("properties", {}).items():
        if nombre in props and esquema.get("description"):
            props[nombre]["description"] = esquema["description"]


def crear_servidor_autonomo() -> MCPServer:
    """Servidor MCP que crea su propio cliente de datos.gov.co y catálogo al arrancar (modo stdio)."""
    estado: dict[str, HerramientasIPS] = {}

    @asynccontextmanager
    async def lifespan(_servidor):
        cliente = datos_gov.ClienteDatosGov()
        estado["h"] = HerramientasIPS(cliente, await datos_gov.cargar_catalogo(cliente))
        try:
            yield
        finally:
            await cliente.aclose()

    mcp = MCPServer("ips", instructions=INSTRUCCIONES, lifespan=lifespan)
    plantilla = HerramientasIPS(None, None)  # solo para leer firmas y docstrings
    for t in plantilla.tools():
        mcp.add_tool(_delegar(t.name, estado, inspect.signature(t.fn)), name=t.name, description=t.description)
        _copiar_descripciones(mcp, t)
    return mcp


def _delegar(nombre: str, estado: dict, firma: inspect.Signature):
    """Función con la MISMA firma que la tool, que llama a la instancia creada en el lifespan."""
    async def llamar(**kwargs):
        return await getattr(estado["h"], nombre)(**kwargs)

    llamar.__name__ = nombre
    llamar.__signature__ = firma
    llamar.__annotations__ = {p.name: p.annotation for p in firma.parameters.values()} | {"return": str}
    return llamar


if __name__ == "__main__":
    crear_servidor_autonomo().run()  # stdio
