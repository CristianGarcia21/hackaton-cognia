# MCP en Cognia

> **Estado:** no está implementado en el código, a propósito. Esta guía deja la receta lista y
> **probada** (SDK `mcp` 2.3, octubre de 2026) para agregarlo en minutos si el reto lo pide.

MCP (Model Context Protocol) es un estándar para que los modelos usen herramientas y datos externos.
Hay dos direcciones posibles:

| Dirección | Qué significa | Cuándo hacerlo |
|---|---|---|
| **A. Cliente** (consumir MCP) | Tus agentes usan las tools de un servidor MCP (BD, GitHub, archivos, una API de la empresa...) | Si el reto te da servidores MCP o pide integrar uno. **Es lo más probable.** |
| **B. Servidor** (exponer MCP) | Claude Desktop, Cursor u otro cliente usan *tus* agentes como herramienta | Solo si el reto pide "integra tu agente en X" |

## Cómo encaja en la arquitectura

MCP es un **adaptador de salida** (cliente) o un **adaptador de entrada** (servidor). El núcleo no cambia:

```
Cliente:   Agent ──usa──▶ Tool ◀──envuelve── core/tools/mcp.py ──▶ servidor MCP externo
Servidor:  cliente MCP externo ──▶ mcp_server.py ──▶ Supervisor/Agent (mismos ejecutores)
```

Un servidor MCP entrega una lista de tools con nombre, descripción y JSON Schema, que es justo lo que
contiene nuestro `Tool`. Por eso el adaptador solo traduce, y los agentes reciben esas tools igual que
`web_search`.

---

## A. Cliente: usar servidores MCP desde tus agentes

### 1. Instalar

```bash
uv add mcp
```

> El SDK 2.x cambió nombres respecto a la 1.x: `FastMCP` ahora es `MCPServer` y hay una clase
> `mcp.Client` de alto nivel. Si encuentras ejemplos con `from mcp.server.fastmcp import FastMCP`,
> son de la versión 1.

### 2. Crear el adaptador `core/tools/mcp.py`

Copia este archivo tal cual:

```python
"""Adaptador MCP: convierte las herramientas de un servidor MCP en Tool de Cognia.

    from core.tools.mcp import MCPConnection
    fs = MCPConnection.stdio("npx", ["-y", "@modelcontextprotocol/server-filesystem", "."])
    agente = Agent("archivos", "...", tools=fs.tools())

El SDK de MCP es asíncrono y nuestros agentes son síncronos: la conexión vive en un
event loop en un hilo aparte y cada llamada se despacha a ese loop y se espera.
"""

import asyncio
import threading
from concurrent.futures import Future
from typing import Any

from mcp import Client, StdioServerParameters

from core.tools import Tool


class MCPConnection:
    def __init__(self, target: Any, prefix: str = ""):
        self.prefix = prefix
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._closing: asyncio.Event | None = None
        ready: Future = Future()
        self._task = asyncio.run_coroutine_threadsafe(self._lifecycle(target, ready), self._loop)
        ready.result(timeout=60)  # espera la conexión (o relanza el error si falló)

    async def _lifecycle(self, target: Any, ready: Future) -> None:
        # anyio exige abrir y cerrar la conexión en la MISMA tarea: esta tarea la mantiene viva.
        self._closing = asyncio.Event()
        try:
            async with Client(target) as client:
                self._client = client
                ready.set_result(True)
                await self._closing.wait()
        except BaseException as e:
            if not ready.done():
                ready.set_exception(e)
            raise

    @classmethod
    def stdio(cls, command: str, args: list[str] | None = None, env: dict | None = None, prefix: str = ""):
        """Servidor local que se lanza como proceso (lo más común: npx, uvx, python...)."""
        return cls(StdioServerParameters(command=command, args=args or [], env=env), prefix)

    @classmethod
    def http(cls, url: str, prefix: str = ""):
        """Servidor remoto por Streamable HTTP, p. ej. "http://localhost:8000/mcp"."""
        return cls(url, prefix)

    def _run(self, coro, timeout: float = 120):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def tools(self, only: list[str] | None = None) -> list[Tool]:
        """Herramientas del servidor como Tool. `only` filtra por nombre (menos tools = agente más preciso)."""
        listed = self._run(self._client.list_tools()).tools
        return [self._wrap(t) for t in listed if not only or t.name in only]

    def _wrap(self, t) -> Tool:
        def call(**kwargs) -> str:
            result = self._run(self._client.call_tool(t.name, kwargs))
            parts = [getattr(c, "text", None) or f"[{c.type}]" for c in getattr(result, "content", [])]
            text = "\n".join(parts) or str(getattr(result, "structured_content", "") or "(sin contenido)")
            return f"Error: {text}" if getattr(result, "is_error", False) else text

        return Tool(name=f"{self.prefix}{t.name}", description=t.description or t.name, fn=call,
                    parameters=t.input_schema or {"type": "object", "properties": {}})

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._closing.set)
        self._task.result(timeout=30)
        self._loop.call_soon_threadsafe(self._loop.stop)
```

**Por qué está hecho así:**
- El SDK es `async` y `Agent.run` es síncrono. La conexión vive en un event loop en un hilo aparte y
  cada llamada se despacha con `run_coroutine_threadsafe`, así que no hubo que tocar `agent.py`.
- La conexión se abre una sola vez y se reutiliza. Abrir un proceso por cada llamada sería muy lento.
- La apertura y el cierre ocurren dentro de la **misma tarea** (`_lifecycle`). Si no, `anyio` lanza
  `RuntimeError: Attempted to exit cancel scope in a different task`.
- Los errores del servidor se devuelven como texto `"Error: ..."`, siguiendo el contrato de las tools.

### 3. Usarlo en un equipo (`agents/reto.py`)

```python
import shutil
from core.tools.mcp import MCPConnection

# Se conecta una sola vez, al importar el módulo (no dentro de reto_team, que se llama en cada mensaje).
_fs = MCPConnection.stdio(shutil.which("npx") or "npx",  # en Windows resuelve npx.cmd
                          ["-y", "@modelcontextprotocol/server-filesystem", "./data"], prefix="fs_")
FS_TOOLS = _fs.tools(only=["read_text_file", "list_directory", "search_files"])

def reto_team(model=None, kb=None):
    return [
        Agent("archivista", "Gestionas los archivos del proyecto con tus herramientas fs_*. ...",
              tools=FS_TOOLS, model=model, description="Lee y busca archivos."),
        ...
    ]
```

Variantes de conexión:

```python
MCPConnection.stdio("uvx", ["mcp-server-fetch"])                 # servidor Python publicado
MCPConnection.stdio("python", ["ruta/servidor.py"], env={"API_KEY": os.getenv("X_KEY")})
MCPConnection.http("https://servidor-del-reto.com/mcp")          # servidor remoto (Streamable HTTP)
```

### 4. Buenas prácticas

- **Filtra con `only=[...]`.** Hay servidores que exponen más de 30 tools, y con tantas el modelo
  elige peor y gasta más tokens.
- **Usa `prefix`** si conectas varios servidores, para evitar choques de nombres (`gh_`, `fs_`, `db_`...).
- **En las instrucciones del agente, di cuándo usar cada tool MCP.** Las descripciones de los
  servidores suelen ser genéricas.
- **Secrets:** pásalos con `env=` leyendo del `.env`. Nunca en el código.
- Los servidores con `npx` requieren Node.js; los que usan `uvx` solo necesitan uv.
- Para probar un servidor sin agentes:
  ```python
  c = MCPConnection.stdio(...); t = c.tools(); print([x.name for x in t]); print(t[0](arg="..."))
  ```

---

## B. Servidor: exponer tus agentes por MCP

Crea `mcp_server.py` en la raíz. Es un **adaptador de entrada**, al mismo nivel que `app.py`, y
reutiliza los mismos ejecutores:

```python
"""Expone el equipo del reto como servidor MCP.  Ejecutar: uv run python mcp_server.py"""
from mcp.server.mcpserver import MCPServer

from agents import TEAMS
from core.orchestrator import Supervisor

mcp = MCPServer("cognia", instructions="Equipo de agentes de IA para <describe el reto>.")

@mcp.tool()
def preguntar_equipo(tarea: str) -> str:
    """Envía una tarea al equipo de agentes y devuelve la respuesta final."""
    return Supervisor(TEAMS["Reto"]()).run(tarea)

if __name__ == "__main__":
    mcp.run()                              # stdio: para Claude Desktop / Cursor
    # mcp.run("streamable-http")           # HTTP: para clientes remotos
```

Para conectarlo a Claude Desktop o Cursor, agrégalo a su configuración de servidores MCP:

```json
{
  "mcpServers": {
    "cognia": {
      "command": "uv",
      "args": ["run", "--directory", "D:/Hackaton/cognia", "python", "mcp_server.py"]
    }
  }
}
```

Para probarlo sin cliente externo (en memoria, sin procesos):

```python
import asyncio
from mcp import Client
from mcp_server import mcp

async def main():
    async with Client(mcp) as c:
        print([t.name for t in (await c.list_tools()).tools])
        print((await c.call_tool("preguntar_equipo", {"tarea": "..."})).content[0].text)

asyncio.run(main())
```

> En modo `stdio`, **nada puede imprimir a stdout** porque rompe el protocolo. Los logs van a stderr,
> que es lo que hace `logging` por defecto.

---

## Verificación hecha

- **Cliente:** servidor MCP de prueba (stdio) con las tools `stock` y `precio` → adaptador → `Agent`
  con Groq `gpt-oss-120b`. El agente llamó `inv_stock` e `inv_precio` y respondió *"12 laptops,
  10 788 USD"*. La conexión se cerró limpia.
- **Servidor:** `MCPServer` con la tool `preguntar_equipo` → `Client` en memoria → Supervisor con el
  equipo base. Respondió *"2^10 = 1024"*.
