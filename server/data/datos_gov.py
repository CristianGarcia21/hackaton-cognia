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


def patron_like(valor: str) -> str:
    """Literal para `upper(campo) like <patrón>`: quita los comodines % y _ (SoQL no tiene ESCAPE fiable)
    para que 'Clínica_%' no devuelva todo el dataset. 'san vicente' → '%SAN VICENTE%'."""
    limpio = " ".join(re.sub(r"[%_]", " ", str(valor)).split()).upper()
    return texto(f"%{limpio}%")


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
        self.presupuesto = timeout + 1.5  # tope de TODA la consulta (reintentos incluidos): voz en vivo
        self._cache: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()
        self._en_vuelo: dict[str, asyncio.Future] = {}
        self.metricas = {"aciertos": 0, "fallos": 0, "ms_ultima": 0.0}

    async def consultar(self, soql: str, limite: int = 1000) -> list[dict]:
        """Ejecuta una consulta SoQL (con caché). Lanza FuenteNoDisponible o ConsultaInvalida.
        Devuelve una copia: modificar el resultado no altera la caché."""
        clave = f"{limite}|{soql}"
        guardado = self._cache.get(clave)
        if guardado and time.monotonic() - guardado[0] < self.ttl:
            self._cache.move_to_end(clave)
            self.metricas["aciertos"] += 1
            return [dict(f) for f in guardado[1]]

        futuro = self._en_vuelo.get(clave)
        if futuro is None:  # nadie la está pidiendo: lanzarla y compartirla con quien llegue después
            self.metricas["fallos"] += 1
            futuro = asyncio.ensure_future(self._pedir(soql, limite))
            self._en_vuelo[clave] = futuro
            futuro.add_done_callback(lambda f, c=clave, n=limite, q=soql: self._al_terminar(c, n, q, f))
        # shield para TODOS: si un solicitante se cancela (interrupción de voz), la consulta sigue y los
        # demás reciben su resultado; además el resultado queda en caché.
        return [dict(f) for f in await asyncio.shield(futuro)]

    def _al_terminar(self, clave: str, limite: int, soql: str, futuro: asyncio.Future) -> None:
        self._en_vuelo.pop(clave, None)
        if futuro.cancelled() or futuro.exception() is not None:  # leer la excepción evita avisos de asyncio
            return
        filas = futuro.result()
        if len(filas) >= limite:
            log.warning("Resultado truncado a %s filas: %s", limite, soql[:120])
        self._cache[clave] = (time.monotonic(), filas)
        self._cache.move_to_end(clave)
        while len(self._cache) > self.max_cache:
            self._cache.popitem(last=False)

    async def _pedir(self, soql: str, limite: int) -> list[dict]:
        try:
            async with asyncio.timeout(self.presupuesto):
                return await self._intentos(soql, limite)
        except TimeoutError:
            raise FuenteNoDisponible("datos.gov.co no responde (tiempo agotado)") from None

    async def _intentos(self, soql: str, limite: int) -> list[dict]:
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
                try:
                    return r.json()
                except ValueError:  # p. ej. página HTML de mantenimiento con 200
                    raise FuenteNoDisponible("datos.gov.co devolvió una respuesta no válida") from None
            if r.status_code in (401, 403) and self.auth:
                log.warning("datos.gov.co rechazó la API key (%s); sigo sin autenticación", r.status_code)
                self.auth = None
                continue  # reintento inmediato sin auth (no cuenta como intento; ocurre una sola vez)
            if r.status_code == 400:
                raise ConsultaInvalida(f"datos.gov.co rechazó la consulta: {r.text[:300]}")
            if r.status_code == 429:  # reintentar de inmediato solo empeora el límite
                raise FuenteNoDisponible("datos.gov.co: límite de uso alcanzado, intenta en unos segundos")
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
_CONECTORES = {"el", "la", "los", "las", "de", "del", "y", "san", "santa", "norte", "sur", "en"}

# Palabras que nombran un GRUPO de capacidad: restringen la búsqueda a ese grupo ("sillas de salud mental").
GRUPOS_DICHOS: list[tuple[tuple[str, ...], str]] = [
    (("ambulancia", "ambulancias"), "AMBULANCIAS"),
    (("unidad movil", "unidades moviles"), "UNIDAD MOVIL"),
    (("consultorio", "consultorios"), "CONSULTORIOS"),
    (("camilla", "camillas"), "CAMILLAS"),
    (("cama", "camas", "hospitalizacion"), "CAMAS"),
    (("silla", "sillas"), "SILLAS"),
    (("sala", "salas"), "SALAS"),
]

# Sinónimos de TIPOS de capacidad, del más específico al más general: (frases dichas, raíces a buscar en
# el nombre normalizado del tipo). Se aplican dentro del grupo dicho, si lo hay, y luego los modificadores.
SINONIMOS_CAPACIDAD: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("cuidado intermedio", "cuidados intermedios", "intermedio", "intermedia", "intermedios", "intermedias"),
     ("intermedi",)),
    (("uci", "cuidado intensivo", "cuidados intensivos", "terapia intensiva", "intensiva", "intensivo",
      "intensivas", "intensivos"), ("intensiv",)),
    (("cirugia", "cirugias", "quirofano", "quirofanos", "operacion", "operar"), ("sala de cirugia", "quirofano")),
    (("hemodialisis", "dialisis"), ("hemodialisis",)),
    (("quimio", "quimioterapia"), ("quimioterapia",)),
    (("radioterapia",), ("radioterapia",)),
    (("salud mental", "psiquiatria", "psiquiatrica", "psiquiatricas", "psiquiatrico", "psiquiatricos"),
     ("salud mental", "psiquiatr", "agudo mental")),
    (("quemado", "quemados", "quemaduras"), ("quemad",)),
    (("neonatal", "neonatales", "incubadora", "incubadoras", "recien nacido", "recien nacidos"), ("neonat",)),
    (("parto", "partos", "maternidad"), ("parto",)),
    (("urgencia", "urgencias", "emergencia", "emergencias"), ("urgenc",)),
    (("observacion",), ("observacion",)),
    (("consulta", "consulta externa"), ("consulta externa",)),
    (("pediatria", "pediatrica", "pediatricas", "pediatrico", "pediatricos", "ninos"), ("pediatr",)),
]

# Modificadores que refinan un resultado ("UCI pediátrica" → solo las pediátricas).
MODIFICADORES: list[tuple[tuple[str, ...], str]] = [
    (("pediatria", "pediatrica", "pediatricas", "pediatrico", "pediatricos", "ninos", "nino", "infantil"), "pediatr"),
    (("adulto", "adultos", "adulta", "adultas"), "adult"),
    (("neonatal", "neonatales", "recien nacido", "recien nacidos", "bebe", "bebes"), "neonat"),
]


@dataclass
class Resolucion:
    """Lo que el usuario dijo → filtros EXACTOS para la consulta.

    filtros es un OR de ANDs: [{"nom_grupo_capacidad": "CAMAS", "nom_descripcion_capacidad": "Partos"}, ...]
    o [{"municipio": "ARMENIA", "departamento": "Quindío"}]. Así nunca se mezclan grupos ni homónimos.
    campo/valores son lo principal para mostrar. exacto=False → hay ambigüedad o fue difuso: la tool debe
    confirmar con el usuario usando las sugerencias."""

    consulta: str
    campo: str
    valores: list[str] = field(default_factory=list)
    filtros: list[dict[str, str]] = field(default_factory=list)
    exacto: bool = True
    sugerencias: list[str] = field(default_factory=list)

    def soql(self) -> str:
        if not self.filtros:
            raise ValueError(f"'{self.consulta}' no se resolvió a ningún valor de {self.campo}")
        grupos: dict[tuple, list[str]] = {}
        for f in self.filtros:
            resto = tuple(sorted((k, v) for k, v in f.items() if k != self.campo))
            grupos.setdefault(resto, [])
            if self.campo in f and f[self.campo] not in grupos[resto]:
                grupos[resto].append(f[self.campo])
        partes = []
        for resto, valores in grupos.items():
            condiciones = [f"{k} = {texto(v)}" for k, v in resto]
            if valores:
                condiciones.append(en(self.campo, sorted(valores)))
            partes.append(" AND ".join(condiciones))
        if len(partes) == 1:
            return partes[0] if not next(iter(grupos)) else f"({partes[0]})"
        return "(" + " OR ".join(f"({p})" for p in partes) + ")"


def _contiene_frase(texto_n: str, frase: str) -> bool:
    return bool(frase) and re.search(rf"\b{re.escape(frase)}\b", texto_n) is not None


@dataclass
class Catalogo:
    total_filas: int
    municipios: list[tuple[str, str]]          # (municipio, departamento) tal como vienen de la API
    capacidades: list[tuple[str, str]]         # (grupo, tipo)

    def __post_init__(self) -> None:
        self._depto: dict[str, set[str]] = {}       # departamento real normalizado → valores de la API
        self._depto_nombre: dict[str, str] = {}
        self._depto_api: dict[str, str] = {}        # valor de la API normalizado ("cali") → valor
        self._muni: dict[str, set[tuple[str, str]]] = {}  # municipio normalizado → {(municipio, depto API)}
        for muni, depto in self.municipios:
            real = DEPARTAMENTO_REAL.get(normalizar(depto), depto)
            self._depto.setdefault(normalizar(real), set()).add(depto)
            self._depto_nombre[normalizar(real)] = real
            self._depto_api[normalizar(depto)] = depto
            self._muni.setdefault(normalizar(muni), set()).add((muni, depto))
        self._grupos = {normalizar(g): g for g, _ in self.capacidades}

    def stats(self) -> dict:
        return {"registros": self.total_filas, "departamentos": len(self._depto),
                "municipios": len(self.municipios), "tipos_capacidad": len(self.capacidades),
                "grupos_capacidad": sorted(set(self._grupos.values()))}

    def nombre_departamento(self, valor_api: str) -> str:
        """'Cali' → 'Valle del Cauca' (para mostrar)."""
        return DEPARTAMENTO_REAL.get(normalizar(valor_api), valor_api)

    @staticmethod
    def _limpiar(dicho: str) -> str:
        q = normalizar(dicho)
        while re.match(_ARTICULOS, q):
            q = re.sub(_ARTICULOS, "", q)
        return q

    @staticmethod
    def _difuso(q: str, opciones: list[str], umbral: int) -> tuple[list[str], list[str]]:
        """(coincidencias por encima del umbral, top-3 sugerencias)."""
        if not q or not opciones:
            return [], []
        ranking = process.extract(q, opciones, scorer=fuzz.WRatio, limit=3)
        mejores = [o for o, s, _ in ranking if s >= umbral and s >= ranking[0][1] - 2]
        return mejores, [o for o, _, _ in ranking]

    # ---------- departamento ----------

    def resolver_departamento(self, dicho: str) -> Resolucion:
        completo, q = normalizar(dicho), self._limpiar(dicho)
        claves = list(self._depto)
        for candidato in dict.fromkeys((completo, q)):  # primero tal cual ("La Guajira"), luego sin artículos
            if candidato in self._depto:
                return self._res_depto(dicho, [candidato])
            if candidato in self._depto_api:  # un distrito pedido como tal: "Cali" → solo Cali
                v = self._depto_api[candidato]
                return Resolucion(dicho, "departamento", [v], [{"departamento": v}])
        if not q or q in _CONECTORES or len(q) < 3:  # "la", "de", "san": no adivinar
            return Resolucion(dicho, "departamento", exacto=False)
        if len(q) >= 4:  # "el valle" → "valle del cauca"
            parciales = [k for k in claves if k.startswith(q + " ") or q in k.split()]
            if parciales:
                return self._res_depto(dicho, parciales, exacto=len(parciales) == 1)
        mejores, sug = self._difuso(q, claves, 85)
        res = self._res_depto(dicho, mejores, exacto=False)
        res.sugerencias = [self._depto_nombre[s] for s in sug]
        return res

    def _res_depto(self, dicho: str, claves: list[str], exacto: bool = True) -> Resolucion:
        valores = sorted(set().union(*(self._depto[k] for k in claves))) if claves else []
        return Resolucion(dicho, "departamento", valores, [{"departamento": v} for v in valores], exacto,
                          sugerencias=[self._depto_nombre[k] for k in claves] if not exacto else [])

    # ---------- municipio ----------

    def resolver_municipio(self, dicho: str, departamento: str | None = None) -> Resolucion:
        completo, q = normalizar(dicho), self._limpiar(dicho)
        exacto = True
        clave = next((c for c in dict.fromkeys((completo, q)) if c in self._muni), None)
        if clave:
            pares = set(self._muni[clave])
        else:
            mejores, sug = self._difuso(q, list(self._muni), 80)
            pares = set().union(*(self._muni[k] for k in mejores)) if mejores else set()
            exacto = False
            if not pares:
                return Resolucion(dicho, "municipio", exacto=False, sugerencias=[
                    self._etiqueta(p) for s in sug for p in sorted(self._muni[s])][:3])

        if departamento:
            permitidos = set(self.resolver_departamento(departamento).valores)
            filtrados = {p for p in pares if p[1] in permitidos}
            if filtrados:
                pares = filtrados
            else:  # el departamento dicho no tiene ese municipio: no adivinar
                return Resolucion(dicho, "municipio", exacto=False,
                                  sugerencias=[self._etiqueta(p) for p in sorted(pares)][:3])
        reales = {self.nombre_departamento(d) for _, d in pares}
        if len(reales) > 1:  # homónimos en varios departamentos (ARMENIA, LA UNIÓN...): pedir aclaración
            exacto = False
        pares_ord = sorted(pares)
        return Resolucion(dicho, "municipio", sorted({m for m, _ in pares_ord}),
                          [{"municipio": m, "departamento": d} for m, d in pares_ord], exacto,
                          sugerencias=[self._etiqueta(p) for p in pares_ord] if not exacto else [])

    def _etiqueta(self, par: tuple[str, str]) -> str:
        return f"{par[0]} ({self.nombre_departamento(par[1])})"

    # ---------- capacidad ----------

    @staticmethod
    def _refinar(q: str, pares: list[tuple[str, str]], raices_base: tuple[str, ...] = ()) -> list[tuple[str, str]]:
        """Aplica modificadores dichos ("pediátrica", "adultos"...) salvo los que ya son la regla base.
        Si el filtro deja la lista vacía, se conserva la original (mejor amplio que nada)."""
        for frases, raiz in MODIFICADORES:
            if raiz in raices_base or not any(_contiene_frase(q, f) for f in frases):
                continue
            pares = [p for p in pares if raiz in normalizar(p[1])] or pares
        return pares

    def _res_capacidad(self, dicho: str, pares: list[tuple[str, str]], exacto: bool = True,
                       sugerencias: list[str] | None = None) -> Resolucion:
        pares = sorted(set(pares))
        return Resolucion(dicho, "nom_descripcion_capacidad", sorted({t for _, t in pares}),
                          [{"nom_grupo_capacidad": g, "nom_descripcion_capacidad": t} for g, t in pares],
                          exacto, sugerencias or [])

    def resolver_capacidad(self, dicho: str) -> Resolucion:
        q = self._limpiar(dicho)
        grupo = next((g for frases, g in GRUPOS_DICHOS
                      if normalizar(g) in self._grupos and any(_contiene_frase(q, f) for f in frases)), None)
        pares = [p for p in self.capacidades if grupo is None or p[0] == grupo]

        # 1. Sinónimos de tipo ("UCI", "quirófano"...) dentro del grupo dicho, refinados por modificadores.
        for frases, raices in SINONIMOS_CAPACIDAD:
            if any(_contiene_frase(q, f) for f in frases):
                tipos = [p for p in pares if any(r in normalizar(p[1]) for r in raices)]
                tipos = self._refinar(q, tipos, raices)
                if tipos:
                    return self._res_capacidad(dicho, tipos)
        # 2. Nombre de tipo dicho literalmente ("ambulancia medicalizada", "camas de obstetricia").
        contenidos = [p for p in pares if _contiene_frase(q, normalizar(p[1]))]
        contenidos = [p for p in contenidos  # quitar los que están incluidos en otro más largo
                      if not any(o != p and normalizar(p[1]) in normalizar(o[1]) for o in contenidos)]
        if contenidos:
            return self._res_capacidad(dicho, contenidos)
        # 3. Solo el grupo ("camas", "ambulancias"), refinado por modificadores ("camas pediátricas").
        if grupo:
            refinados = self._refinar(q, pares)
            if len(refinados) < len(pares):
                return self._res_capacidad(dicho, refinados)
            return Resolucion(dicho, "nom_grupo_capacidad", [grupo], [{"nom_grupo_capacidad": grupo}])
        # 4. Difuso sobre los nombres de tipo.
        por_nombre: dict[str, list[tuple[str, str]]] = {}
        for p in pares:
            por_nombre.setdefault(normalizar(p[1]), []).append(p)
        mejores, sug = self._difuso(q, list(por_nombre), 85)
        return self._res_capacidad(dicho, [p for k in mejores for p in por_nombre[k]], exacto=False,
                                   sugerencias=[por_nombre[s][0][1] for s in sug])


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
