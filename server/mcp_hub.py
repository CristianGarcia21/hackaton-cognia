"""Hub MCP: clientes MCP async hacia cada servidor registrado (spec §4.1, §8, §10.2).

    hub = HubMCP()
    hub.registrar("ips", mcp_servers.ips.crear_servidor(h), cacheable=True)   # en proceso
    hub.registrar("x", StdioServerParameters(command=..., args=[...]))        # subproceso stdio
    hub.registrar("y", "https://.../mcp")                                     # remoto (streamable HTTP)
    await hub.iniciar()
    r = await hub.llamar("buscar_ips", {"municipio": "Cali"})   # Resultado(texto, status, ms, ...)

- **Origen del servidor** (lo que acepta `mcp.Client`): un MCPServer en proceso (por defecto: Render Free
  tiene 512 MB y cada subproceso de Python cuesta ~80 MB), parámetros stdio o una URL. Cambiar uno por
  otro separa un servicio sin tocar al agente (spec §4.2).
- **Supervisión:** una tarea por servidor mantiene abierta la conexión; si se cae (el proceso muere, la
  red falla), la reabre con espera creciente. Mientras tanto la tool responde "no disponible".
- **Nunca lanza:** toda falla (timeout de 3 s, argumentos inválidos, bug, servidor caído) vuelve como
  texto "Error: ..." para que el LLM se corrija o lo diga. CancelledError (barge-in) sí se propaga.
- **Caché por argumentos** solo en servidores `cacheable` (lecturas puras como IPS; nunca citas).
"""

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Literal

from mcp import Client

log = logging.getLogger("cognia.mcp_hub")

Status = Literal["ok", "error", "timeout"]


@dataclass(frozen=True)
class ToolMCP:
    servidor: str
    nombre: str
    descripcion: str
    esquema: dict


@dataclass(frozen=True)
class Resultado:
    texto: str
    status: Status
    ms: float
    servidor: str | None = None
    cache: bool = False


@dataclass
class _Servidor:
    nombre: str
    origen: Any
    cacheable: bool
    cliente: Client | None = None
    tools: dict[str, ToolMCP] = field(default_factory=dict)
    caido: asyncio.Event = field(default_factory=asyncio.Event)
    intento: asyncio.Event = field(default_factory=asyncio.Event)  # terminó el primer intento de conexión
    tarea: asyncio.Task | None = None


class HubMCP:
    def __init__(self, timeout_s: float = 3.0, ttl_cache_s: float = 600.0, max_cache: int = 256,
                 espera_inicial_s: float = 1.0, espera_max_s: float = 30.0):
        self.timeout_s, self.ttl_cache_s, self.max_cache = timeout_s, ttl_cache_s, max_cache
        self.espera_inicial_s, self.espera_max_s = espera_inicial_s, espera_max_s
        self._servidores: dict[str, _Servidor] = {}
        self._indice: dict[str, str] = {}  # tool -> servidor (la primera que se registró gana)
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._cerrando = False

    # ------------------------------- ciclo de vida -------------------------------

    def registrar(self, nombre: str, origen, *, cacheable: bool = False) -> None:
        """Agrega un servidor. `cacheable=True` solo para tools de lectura sin efectos."""
        if nombre in self._servidores:
            raise ValueError(f"El servidor MCP '{nombre}' ya está registrado")
        self._servidores[nombre] = _Servidor(nombre, origen, cacheable)

    async def iniciar(self, espera_s: float = 15.0) -> None:
        """Lanza la supervisión de cada servidor y espera el primer intento de todos (con tope).
        Un servidor que no arranca no impide los demás: queda reintentando en segundo plano."""
        self._cerrando = False
        for s in self._servidores.values():
            if s.tarea is None:
                s.tarea = asyncio.create_task(self._supervisar(s), name=f"mcp:{s.nombre}")
        intentos = [s.intento.wait() for s in self._servidores.values()]
        try:
            async with asyncio.timeout(espera_s):
                await asyncio.gather(*intentos)
        except TimeoutError:
            log.warning("Servidores MCP sin conectar tras %ss: %s", espera_s,
                        [n for n, ok in self.estado().items() if not ok])

    async def cerrar(self) -> None:
        self._cerrando = True
        tareas = [s.tarea for s in self._servidores.values() if s.tarea]
        for t in tareas:
            t.cancel()
        await asyncio.gather(*tareas, return_exceptions=True)
        for s in self._servidores.values():
            s.tarea, s.cliente = None, None

    async def _supervisar(self, s: _Servidor) -> None:
        espera = self.espera_inicial_s
        while not self._cerrando:
            try:
                async with Client(s.origen) as cliente:
                    self._indexar(s, (await cliente.list_tools()).tools)
                    s.caido.clear()
                    s.cliente = cliente
                    s.intento.set()
                    log.info("MCP '%s' conectado (%d tools)", s.nombre, len(s.tools))
                    espera = self.espera_inicial_s
                    await s.caido.wait()  # una llamada detectó que la conexión murió: reabrir
                    log.warning("MCP '%s' caído; reconectando", s.nombre)
            except asyncio.CancelledError:
                raise
            except BaseException as e:  # noqa: BLE001 — ExceptionGroup de anyio incluido: reintentar
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                log.warning("MCP '%s' no disponible (%s); reintento en %.1fs", s.nombre, _causa(e), espera)
            finally:
                s.cliente = None
                s.intento.set()
            if self._cerrando:
                break
            await asyncio.sleep(espera)
            espera = min(espera * 2, self.espera_max_s)

    def _indexar(self, s: _Servidor, tools) -> None:
        s.tools = {}
        for t in tools:
            dueño = self._indice.setdefault(t.name, s.nombre)
            if dueño != s.nombre:
                log.error("Tool '%s' de '%s' ignorada: ya la expone '%s'", t.name, s.nombre, dueño)
                continue
            s.tools[t.name] = ToolMCP(s.nombre, t.name, t.description or t.name, dict(t.input_schema or {}))

    # ------------------------------- consulta -------------------------------

    def estado(self) -> dict[str, bool]:
        return {n: s.cliente is not None for n, s in self._servidores.items()}

    def listo(self) -> bool:
        return bool(self._servidores) and all(self.estado().values())

    def tools(self) -> list[ToolMCP]:
        """Tools conocidas (se conservan aunque su servidor esté caído: el agente ya las declaró)."""
        return [t for s in self._servidores.values() for t in s.tools.values()]

    async def llamar(self, nombre: str, args: dict | None = None) -> Resultado:
        inicio = time.perf_counter()
        args = {k: v for k, v in (args or {}).items() if v is not None}

        def fin(texto: str, status: Status, servidor=None, cache=False) -> Resultado:
            if status == "ok" and texto.startswith("Error:"):
                status = "error"  # la tool explicó un problema: no es un resultado válido
            return Resultado(texto, status, round((time.perf_counter() - inicio) * 1000, 1), servidor, cache)

        s = self._servidores.get(self._indice.get(nombre, ""))
        if s is None:
            disponibles = ", ".join(t.nombre for t in self.tools()) or "ninguna"
            return fin(f"Error: no existe la tool '{nombre}'. Disponibles: {disponibles}.", "error")

        clave = f"{nombre}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
        if s.cacheable and (texto := self._de_cache(clave)) is not None:
            return fin(texto, "ok", s.nombre, cache=True)

        cliente = s.cliente
        if cliente is None:
            return fin(f"Error: el servicio '{s.nombre}' no está disponible en este momento; ya estoy "
                       "reconectando. Intenta de nuevo en unos segundos.", "error", s.nombre)
        try:
            async with asyncio.timeout(self.timeout_s):
                r = await cliente.call_tool(nombre, args)
        except TimeoutError:
            return fin(f"Error: la consulta tardó más de {self.timeout_s:g} s. Puedo intentarlo de nuevo "
                       "o buscarlo de otra forma.", "timeout", s.nombre)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — la conexión murió: que el supervisor la reabra
            log.warning("MCP '%s' falló llamando %s: %s", s.nombre, nombre, _causa(e))
            if s.cliente is cliente:  # nadie más debe usar esta conexión muerta mientras se reabre
                s.cliente = None
                s.caido.set()
            return fin(f"Error: el servicio '{s.nombre}' no está disponible en este momento; ya estoy "
                       "reconectando. Intenta de nuevo en unos segundos.", "error", s.nombre)

        texto = "\n".join(c.text for c in r.content if getattr(c, "text", None)).strip()
        if r.is_error:
            return fin(_error_corto(nombre, texto), "error", s.nombre)
        if s.cacheable and not texto.startswith("Error:"):
            self._a_cache(clave, texto)
        return fin(texto, "ok", s.nombre)

    def _de_cache(self, clave: str) -> str | None:
        guardado = self._cache.get(clave)
        if guardado is None:
            return None
        if time.monotonic() - guardado[0] > self.ttl_cache_s:
            del self._cache[clave]
            return None
        self._cache.move_to_end(clave)
        return guardado[1]

    def _a_cache(self, clave: str, texto: str) -> None:
        self._cache[clave] = (time.monotonic(), texto)
        self._cache.move_to_end(clave)
        while len(self._cache) > self.max_cache:
            self._cache.popitem(last=False)


def _causa(e: BaseException) -> str:
    """Primera excepción real dentro de un ExceptionGroup de anyio (para un log legible)."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}"[:200]


def _error_corto(nombre: str, texto: str) -> str:
    """Convierte el error del SDK en una frase corta que el LLM pueda usar para corregirse
    (sin URLs de Pydantic ni trazas)."""
    if "validation error" in texto:
        lineas = texto.splitlines()[1:]
        problemas = []
        for campo, detalle in zip(lineas, lineas[1:]):
            if campo and not campo.startswith(" ") and detalle.startswith("  "):
                problemas.append(f"{campo}: {re.sub(r'\s*\[type=.*$', '', detalle.strip())}")
        return f"Error: argumentos inválidos para {nombre}: {'; '.join(problemas)[:200]}. Corrígelos y vuelve a intentar."
    return f"Error: la tool {nombre} falló. Prueba de nuevo o reformula la pregunta."
