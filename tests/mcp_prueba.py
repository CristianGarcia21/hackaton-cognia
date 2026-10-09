"""Servidor MCP mínimo para los tests del hub (stdio): eco, lento y morir (simula que el proceso se cae)."""

import asyncio
import os

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("prueba")


async def eco(texto: str, veces: int = 1) -> str:
    """Repite un texto.

    Args:
        texto: lo que se repite
        veces: cuántas veces
    """
    return texto * veces


async def lento(segundos: float) -> str:
    """Tarda los segundos indicados."""
    await asyncio.sleep(segundos)
    return "listo"


def morir() -> str:
    """Termina el proceso sin responder (simula una caída del servidor)."""
    os._exit(1)


async def morir_luego() -> str:
    """Responde y termina el proceso poco después (una caída sin llamadas en curso)."""
    asyncio.get_running_loop().call_later(0.2, os._exit, 1)
    return "ok"


for _f in (eco, lento, morir, morir_luego):
    mcp.add_tool(_f)

if __name__ == "__main__":
    mcp.run()
