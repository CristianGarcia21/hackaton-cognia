"""Acceso a la API de datos.gov.co bajo demanda (spec §7). NO se descarga ni se guarda el dataset.

- ClienteDatosGov.consultar(soql): POST a SODA3 con auth Basic (DATOS_GOV_KEY_ID/SECRET), conexión
  reutilizada, timeout por intento, un reintento ante 5xx, caché de resultados con TTL y límite de
  tamaño, y deduplicación de consultas idénticas simultáneas. Si la key es rechazada sigue sin auth
  (la API lo permite con límite de uso). Si la API no responde → FuenteNoDisponible.
- Catalogo: valores de referencia traídos DE LA API al arrancar (departamentos, municipios y tipos
  de capacidad, ~1 200 entradas). Sirve de "caché de búsqueda": traduce lo que dijo el usuario (con
  errores del STT, sin tildes, sinónimos como "UCI") a los valores exactos para armar la consulta.
- texto()/en(): construyen SoQL escapado. El LLM nunca escribe SoQL: lo arman las tools (#8).

Calidad de datos que las tools deben tener en cuenta (verificado 2026-10-09):
- ~3 145 filas son duplicados exactos → sumar con un pipe de deduplicación (ver DEDUP en las tools).
- Cali, Buenaventura, Barranquilla, Cartagena y Santa Marta aparecen como "departamento" (distritos):
  resolver_departamento("Valle") devuelve Valle del cauca + Cali + Buenaventura.
- El id real de una sede es c_digo_sede + n_mero_sede.
"""

import asyncio
import inspect
import logging
import math
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from rapidfuzz import fuzz, process

from server import config
from server import events as ev

log = logging.getLogger("cognia.datos")

BASE = "https://www.datos.gov.co"
DATASET = "s2ru-bqt6"
SODA3 = f"/api/v3/views/{DATASET}/query.json"

OnStatus = Callable[[ev.SourceStatus], Any] | None


class FuenteNoDisponible(Exception):
    """datos.gov.co no respondió (caída, timeout, límite de uso). Se informa con honestidad."""


class ConsultaInvalida(Exception):
    """La API rechazó la consulta (400): es un bug de la tool que la armó, no del usuario."""


# ------------------------------- SoQL seguro -------------------------------

def texto(valor: str) -> str:
    """Literal de texto SoQL escapado: O'Neill → 'O''Neill'."""
    return "'" + str(valor).replace("'", "''") + "'"


def en(campo: str, valores: list[str]) -> str:
    return f"{campo} IN ({', '.join(texto(v) for v in valores)})"


def normalizar(valor: Any) -> str:
    """'  Bogotá,  D.C. ' → 'bogota d c'. Para comparar lo que dice el STT con los valores de la API."""
    if valor is None or (isinstance(valor, float) and math.isnan(valor)):
        return ""
    sin_tildes = unicodedata.normalize("NFKD", str(valor)).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", sin_tildes.lower()).split())


# ------------------------------- cliente -------------------------------

class ClienteDatosGov:
    def __init__(self, auth: tuple[str, str] | None | type[...] = ..., http: httpx.AsyncClient | None = None,
                 timeout: float = 3.0, ttl: float = 3600.0, max_cache: int = 512):
        self.auth = config.DATOS_GOV_AUTH if auth is ... else auth
        self._propio = http is None
        self._http = http or httpx.AsyncClient(
            base_url=BASE, timeout=httpx.Timeout(timeout + 2),
            limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=60))
        self.timeout, self.ttl, self.max_cache = timeout, ttl, max_cache
        self._cache: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()
        self._en_vuelo: dict[str, asyncio.Future] = {}
        self.metricas = {"aciertos": 0, "fallos": 0, "ms_ultima": 0.0}

    async def consultar(self, soql: str, limite: int = 1000) -> list[dict]:
        """Ejecuta una consulta SoQL (con caché). Lanza FuenteNoDisponible o ConsultaInvalida."""
        clave = f"{limite}|{soql}"
        guardado = self._cache.get(clave)
        if guardado and time.monotonic() - guardado[0] < self.ttl:
            self._cache.move_to_end(clave)
            self.metricas["aciertos"] += 1
            return guardado[1]
        if clave in self._en_vuelo:  # la misma consulta ya está en camino: esperar su resultado
            return await asyncio.shield(self._en_vuelo[clave])

        self.metricas["fallos"] += 1
        futuro = asyncio.ensure_future(self._pedir(soql, limite))
        self._en_vuelo[clave] = futuro
        try:
            filas = await futuro
        finally:
            self._en_vuelo.pop(clave, None)
        self._cache[clave] = (time.monotonic(), filas)
        self._cache.move_to_end(clave)
        while len(self._cache) > self.max_cache:
            self._cache.popitem(last=False)
        return filas

    async def _pedir(self, soql: str, limite: int) -> list[dict]:
        cuerpo = {"query": soql, "includeSynthetic": False, "page": {"pageNumber": 1, "pageSize": limite}}
        ultimo = "sin respuesta"
        intentos = 0
        while intentos < 2:
            inicio = time.perf_counter()
            try:
                async with asyncio.timeout(self.timeout):
                    r = await self._http.post(SODA3, json=cuerpo, auth=self.auth)
            except (TimeoutError, httpx.TransportError) as e:
                intentos += 1
                ultimo = f"{type(e).__name__}"
                continue
            self.metricas["ms_ultima"] = round((time.perf_counter() - inicio) * 1000, 1)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (401, 403) and self.auth:
                log.warning("datos.gov.co rechazó la API key (%s); sigo sin autenticación", r.status_code)
                self.auth = None
                continue  # reintento inmediato sin auth (no cuenta como intento)
            if r.status_code == 400:
                raise ConsultaInvalida(f"datos.gov.co rechazó la consulta: {r.text[:300]}")
            intentos += 1
            ultimo = f"HTTP {r.status_code}"
        raise FuenteNoDisponible(f"datos.gov.co no responde ({ultimo})")

    async def aclose(self) -> None:
        if self._propio:
            await self._http.aclose()


# ------------------------------- catálogo y resolvedor -------------------------------

# Distritos que el REPS reporta como "departamento" → departamento geográfico real.
DEPARTAMENTO_REAL = {
    "barranquilla": "Atlántico", "cali": "Valle del Cauca", "buenaventura": "Valle del Cauca",
    "cartagena": "Bolívar", "santa marta": "Magdalena", "valle del cauca": "Valle del Cauca",
}
_ARTICULOS = r"^(el|la|los|las|de|del|departamento|depto|dpto|municipio|ciudad|en)\s+"

# Sinónimos de capacidad, del más específico al más general: (frases que dice la gente, regla).
#   ("contiene", x): tipos cuyo nombre normalizado contiene x · ("tipos", [...]): tipos exactos
#   ("grupo", G): grupo de capacidad completo
SINONIMOS_CAPACIDAD: list[tuple[tuple[str, ...], tuple[str, Any]]] = [
    (("uci", "cuidado intensivo", "cuidados intensivos", "terapia intensiva", "intensiva", "intensivo"),
     ("contiene", "intensiv")),
    (("cirugia", "cirugias", "quirofano", "quirofanos", "operacion", "operar"),
     ("tipos", ["sala de cirugia", "quirofano"])),
    (("hemodialisis", "dialisis"), ("contiene", "hemodialisis")),
    (("quimio", "quimioterapia"), ("contiene", "quimioterapia")),
    (("salud mental", "psiquiatria", "psiquiatrico"), ("contiene", "salud mental")),
    (("neonatal", "incubadora", "incubadoras", "recien nacido", "recien nacidos"), ("contiene", "neonatal")),
    (("pediatria", "pediatrica", "pediatrico", "ninos"), ("contiene", "pediatr")),
    (("parto", "partos", "maternidad"), ("contiene", "part")),
    (("urgencia", "urgencias", "emergencia", "emergencias"), ("contiene", "urgenc")),
    (("ambulancia", "ambulancias"), ("grupo", "AMBULANCIAS")),
    (("unidad movil", "unidades moviles"), ("grupo", "UNIDAD MOVIL")),
    (("consultorio", "consultorios", "consulta externa"), ("grupo", "CONSULTORIOS")),
    (("camilla", "camillas"), ("grupo", "CAMILLAS")),
    (("cama", "camas", "hospitalizacion"), ("grupo", "CAMAS")),
    (("silla", "sillas"), ("grupo", "SILLAS")),
    (("sala", "salas"), ("grupo", "SALAS")),
]


# Modificadores que refinan el resultado de un sinónimo ("UCI pediátrica" → solo las pediátricas).
MODIFICADORES: list[tuple[tuple[str, ...], str]] = [
    (("pediatria", "pediatrica", "pediatricas", "pediatrico", "pediatricos", "ninos", "nino", "infantil"), "pediatr"),
    (("adulto", "adultos", "adulta", "adultas"), "adult"),
    (("neonatal", "neonatales", "recien nacido", "recien nacidos", "bebe", "bebes"), "neonat"),
]


@dataclass
class Resolucion:
    """Lo que el usuario dijo → valores exactos de la API para un campo."""

    consulta: str
    campo: str
    valores: list[str] = field(default_factory=list)
    exacto: bool = True
    sugerencias: list[str] = field(default_factory=list)

    def soql(self) -> str:
        if not self.valores:
            raise ValueError(f"'{self.consulta}' no se resolvió a ningún valor de {self.campo}")
        return en(self.campo, self.valores)


def _contiene_frase(texto_n: str, frase: str) -> bool:
    return re.search(rf"\b{re.escape(frase)}\b", texto_n) is not None


@dataclass
class Catalogo:
    total_filas: int
    municipios: list[tuple[str, str]]          # (municipio, departamento) tal como vienen de la API
    capacidades: list[tuple[str, str]]         # (grupo, tipo)

    def __post_init__(self) -> None:
        self._depto: dict[str, set[str]] = {}  # departamento real normalizado → valores de la API
        self._depto_nombre: dict[str, str] = {}
        self._muni: dict[str, set[str]] = {}
        self._muni_depto: dict[str, set[str]] = {}
        for muni, depto in self.municipios:
            real = DEPARTAMENTO_REAL.get(normalizar(depto), depto)
            self._depto.setdefault(normalizar(real), set()).add(depto)
            self._depto_nombre[normalizar(real)] = real
            self._muni.setdefault(normalizar(muni), set()).add(muni)
            self._muni_depto.setdefault(normalizar(muni), set()).add(normalizar(real))
        self._tipos = {normalizar(t): t for _, t in self.capacidades}
        self._grupos = {normalizar(g): g for g, _ in self.capacidades}

    def stats(self) -> dict:
        return {"registros": self.total_filas, "departamentos": len(self._depto),
                "municipios": len(self.municipios), "tipos_capacidad": len(self.capacidades),
                "grupos_capacidad": sorted(set(self._grupos.values()))}

    @staticmethod
    def _limpiar(dicho: str) -> str:
        q = normalizar(dicho)
        while re.match(_ARTICULOS, q):
            q = re.sub(_ARTICULOS, "", q)
        return q

    @staticmethod
    def _difuso(q: str, opciones: list[str], umbral: int) -> tuple[list[str], list[str]]:
        """(coincidencias por encima del umbral, top-3 sugerencias)."""
        ranking = process.extract(q, opciones, scorer=fuzz.WRatio, limit=3)
        mejores = [o for o, s, _ in ranking if s >= umbral and s >= ranking[0][1] - 2]
        return mejores, [o for o, _, _ in ranking]

    def resolver_departamento(self, dicho: str) -> Resolucion:
        q = self._limpiar(dicho)
        claves = list(self._depto)
        exactas = [k for k in claves if k == q] or [k for k in claves if k.startswith(q + " ") or q in k.split()]
        if exactas:
            return Resolucion(dicho, "departamento", sorted(set().union(*(self._depto[k] for k in exactas))))
        mejores, sug = self._difuso(q, claves, 85)
        valores = sorted(set().union(*(self._depto[k] for k in mejores))) if mejores else []
        return Resolucion(dicho, "departamento", valores, exacto=False,
                          sugerencias=[self._depto_nombre[s] for s in sug])

    def resolver_municipio(self, dicho: str, departamento: str | None = None) -> Resolucion:
        q = self._limpiar(dicho)
        claves = list(self._muni)
        if departamento:
            d = self._limpiar(departamento)
            claves = [k for k in claves if any(d in x for x in self._muni_depto[k])] or claves
        if q in claves:
            return Resolucion(dicho, "municipio", sorted(self._muni[q]))
        mejores, sug = self._difuso(q, claves, 80)
        valores = sorted(set().union(*(self._muni[k] for k in mejores))) if mejores else []
        return Resolucion(dicho, "municipio", valores, exacto=False,
                          sugerencias=[next(iter(self._muni[s])) for s in sug])

    @staticmethod
    def _refinar(q: str, tipos: list[str], regla_base: str | None) -> list[str]:
        """Aplica modificadores dichos ("pediátrica", "adultos"...) salvo el que ya es la regla base.
        Si el filtro deja la lista vacía, se conserva la original (mejor amplio que nada)."""
        for frases, raiz in MODIFICADORES:
            if raiz == regla_base or not any(_contiene_frase(q, f) for f in frases):
                continue
            filtrados = [t for t in tipos if raiz in normalizar(t)]
            tipos = filtrados or tipos
        return tipos

    def resolver_capacidad(self, dicho: str) -> Resolucion:
        q = self._limpiar(dicho)
        for frases, (regla, valor) in SINONIMOS_CAPACIDAD:
            if any(_contiene_frase(q, f) for f in frases):
                if regla == "grupo" and normalizar(valor) in self._grupos:
                    return Resolucion(dicho, "nom_grupo_capacidad", [self._grupos[normalizar(valor)]])
                if regla == "contiene":
                    tipos = [t for k, t in self._tipos.items() if valor in k]
                elif regla == "tipos":
                    tipos = [self._tipos[k] for k in valor if k in self._tipos]
                else:
                    tipos = []
                tipos = self._refinar(q, tipos, regla_base=valor if regla == "contiene" else None)
                if tipos:
                    return Resolucion(dicho, "nom_descripcion_capacidad", sorted(tipos))
        if q in self._tipos:
            return Resolucion(dicho, "nom_descripcion_capacidad", [self._tipos[q]])
        if q in self._grupos:
            return Resolucion(dicho, "nom_grupo_capacidad", [self._grupos[q]])
        mejores, sug = self._difuso(q, list(self._tipos), 85)
        return Resolucion(dicho, "nom_descripcion_capacidad", sorted(self._tipos[k] for k in mejores),
                          exacto=False, sugerencias=[self._tipos[s] for s in sug])


async def _emitir(on_status: OnStatus, **campos) -> None:
    if on_status is None:
        return
    r = on_status(ev.SourceStatus(**campos))
    if inspect.isawaitable(r):
        await r


async def cargar_catalogo(cliente: ClienteDatosGov, on_status: OnStatus = None) -> Catalogo:
    """Trae de la API los valores de referencia (3 consultas en paralelo, ~0,4 s). Lanza FuenteNoDisponible."""
    await _emitir(on_status, source="datos.gov.co", status="conectando")
    try:
        total, municipios, capacidades = await asyncio.gather(
            cliente.consultar("SELECT count(*) AS n"),
            cliente.consultar("SELECT departamento, municipio GROUP BY departamento, municipio", limite=5000),
            cliente.consultar("SELECT nom_grupo_capacidad, nom_descripcion_capacidad "
                              "GROUP BY nom_grupo_capacidad, nom_descripcion_capacidad", limite=5000),
        )
    except Exception:
        await _emitir(on_status, source="datos.gov.co", status="error")
        raise
    cat = Catalogo(
        total_filas=int(total[0]["n"]),
        municipios=[(f["municipio"], f["departamento"]) for f in municipios if f.get("municipio")],
        capacidades=[(f["nom_grupo_capacidad"], f["nom_descripcion_capacidad"]) for f in capacidades
                     if f.get("nom_descripcion_capacidad")],
    )
    await _emitir(on_status, source="datos.gov.co", status="listo", rows=cat.total_filas,
                  total_rows=cat.total_filas, pages=1, progress=1)
    log.info("Catálogo de datos.gov.co: %s", cat.stats())
    return cat
