"""Tools de IPS sobre la API de datos.gov.co (spec §8). Contrato de Kognia: core.tools.Tool (@tool).

    h = HerramientasIPS(cliente_datos_gov, catalogo)
    tools = h.tools()            # para el Voice Agent / el hub MCP (mcp_servers/ips.py)

Reglas (spec §8, decisión A): el LLM NUNCA escribe SoQL; estas tools lo arman con los filtros exactos que
da el resolvedor del catálogo. Resultados compactos para voz, errores como texto (nunca excepciones),
sumas SIN duplicados (pipe |>) y unidades separadas por grupo (camas, salas...: no se suman entre sí).
Si lo dicho es ambiguo (homónimos) o no se reconoce, la tool NO consulta: devuelve la aclaración.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from rapidfuzz import fuzz

from core.tools import Tool, tool
from server.data.datos_gov import (Catalogo, ConsultaInvalida, FuenteNoDisponible, normalizar,
                                   patron_sin_tildes, texto)

log = logging.getLogger("cognia.tools.ips")

FUENTE = ("Fuente: datos.gov.co, registro REPS de IPS (corte noviembre de 2022). No incluye horarios, "
          "disponibilidad ni agenda.")
SEDE = ("departamento, municipio, c_digo_sede, n_mero_sede, nom_sede_ips, nombre_prestador, naturaleza, "
        "num_nivel_atencion, direcci_n, tel_fono")
FILA = SEDE + ", nom_grupo_capacidad, nom_descripcion_capacidad, num_cantidad_capacidad_instalada"
UNIDADES = {"CAMAS": ("cama", "camas"), "SALAS": ("sala", "salas"), "CONSULTORIOS": ("consultorio", "consultorios"),
            "AMBULANCIAS": ("ambulancia", "ambulancias"), "CAMILLAS": ("camilla", "camillas"),
            "SILLAS": ("silla", "sillas"), "UNIDAD MOVIL": ("unidad móvil", "unidades móviles")}
AGRUPACIONES = {"departamento": "departamento", "municipio": "municipio, departamento", "naturaleza": "naturaleza",
                "nivel": "num_nivel_atencion", "tipo": "nom_descripcion_capacidad"}
_PALABRAS_VACIAS = {"de", "del", "la", "las", "el", "los", "y", "sede", "ips", "sas", "s", "a", "e", "en"}

Naturaleza = Literal["cualquiera", "Pública", "Privada", "Mixta"]
Nivel = Literal["cualquiera", "1", "2", "3", "sin dato"]
Agrupacion = Literal["ninguno", "departamento", "municipio", "naturaleza", "nivel", "tipo"]


def numero(n: int) -> str:
    """1103 → '1.103' (separador de miles colombiano)."""
    return f"{n:,}".replace(",", ".")


def _entero(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def unidad(grupo: str, n: int) -> str:
    singular, plural = UNIDADES.get(grupo, ("unidad", "unidades"))
    return f"{numero(n)} {singular if n == 1 else plural}"


def sedes(n: int) -> str:
    return f"{numero(n)} sede" + ("" if n == 1 else "s")


_MESES = {"jan": "enero", "feb": "febrero", "mar": "marzo", "apr": "abril", "may": "mayo", "jun": "junio",
          "jul": "julio", "aug": "agosto", "sep": "septiembre", "oct": "octubre", "nov": "noviembre", "dec": "diciembre"}


def fecha_corte(crudo: str) -> str | None:
    """'Fecha corte REPS: Nov  5 2022  1:37PM' → 'corte del 5 de noviembre de 2022'."""
    m = re.search(r"([A-Za-z]{3})\s+(\d{1,2})\s+(\d{4})", crudo or "")
    if not m or m[1].lower() not in _MESES:
        return None
    return f"corte del {int(m[2])} de {_MESES[m[1].lower()]} de {m[3]}"


@dataclass
class _Filtros:
    condiciones: list[str] = field(default_factory=list)
    descripcion: list[str] = field(default_factory=list)  # "en MEDELLÍN (Antioquia)", "con Quirófano..."
    notas: list[str] = field(default_factory=list)        # "Interpreté 'Medeyin' como MEDELLÍN (Antioquia)."
    aclaracion: str | None = None                         # si hay que preguntar antes de consultar
    grupos: set[str] = field(default_factory=set)         # grupos de capacidad filtrados (para las unidades)

    def where(self) -> str:
        return f"WHERE {' AND '.join(self.condiciones)} " if self.condiciones else ""

    def de(self) -> str:
        return (" " + " ".join(self.descripcion)) if self.descripcion else ""


class HerramientasIPS:
    def __init__(self, datos, catalogo: Catalogo, top: int = 5):
        self.datos, self.catalogo, self.top = datos, catalogo, top

    # ------------------------------- filtros -------------------------------

    def _filtrar(self, municipio: str = "", departamento: str = "", capacidad: str = "",
                 naturaleza: str = "cualquiera", nivel: str = "cualquiera") -> _Filtros:
        f, cat = _Filtros(), self.catalogo
        if municipio.strip():
            r = cat.resolver_municipio(municipio, departamento or None)
            if not r.filtros:
                f.aclaracion = (f"No encontré el municipio '{municipio}'"
                                + (f" en {departamento}" if departamento else "")
                                + (f". ¿Quisiste decir: {', '.join(r.sugerencias)}?" if r.sugerencias else "."))
                return f
            lugares = sorted({f"{x['municipio']} ({cat.nombre_departamento(x['departamento'])})" for x in r.filtros})
            if not r.exacto and len(lugares) > 1:
                f.aclaracion = f"Hay varios municipios que coinciden con '{municipio}': {', '.join(lugares)}. ¿Cuál?"
                return f
            if not r.exacto:
                f.notas.append(f"Interpreté '{municipio}' como {lugares[0]}.")
            f.condiciones.append(r.soql())
            f.descripcion.append(f"en {', '.join(lugares)}")
        elif departamento.strip():
            r = cat.resolver_departamento(departamento)
            reales = sorted({cat.nombre_departamento(v) for v in r.valores})
            if not r.filtros:
                f.aclaracion = (f"No reconozco el departamento '{departamento}'"
                                + (f". ¿Quisiste decir: {', '.join(r.sugerencias)}?" if r.sugerencias else "."))
                return f
            if not r.exacto and len(reales) > 1:
                f.aclaracion = f"'{departamento}' puede ser: {', '.join(reales)}. ¿Cuál?"
                return f
            if not r.exacto:
                f.notas.append(f"Interpreté '{departamento}' como {reales[0]}.")
            f.condiciones.append(r.soql())
            f.descripcion.append(f"en {', '.join(reales)}")
        if capacidad.strip():
            r = cat.resolver_capacidad(capacidad)
            if not r.filtros:
                utiles = [s for s in r.sugerencias if fuzz.WRatio(normalizar(capacidad), normalizar(s)) >= 70]
                grupos = ", ".join(g.lower() for g in cat.stats()["grupos_capacidad"])
                f.aclaracion = (f"No reconozco la capacidad '{capacidad}' en estos datos"
                                + (f". ¿Te refieres a: {', '.join(utiles)}?" if utiles else
                                   f": solo registran capacidad instalada de {grupos}."))
                return f
            if not r.exacto:
                f.notas.append(f"Interpreté '{capacidad}' como {', '.join(r.valores)}.")
            f.condiciones.append(r.soql())
            f.grupos = {x["nom_grupo_capacidad"] for x in r.filtros}
            f.descripcion.append(f"con {_lista(r.valores)}")
        if naturaleza and naturaleza != "cualquiera":
            f.condiciones.append(f"naturaleza = {texto(naturaleza)}")
            f.descripcion.append(f"de naturaleza {naturaleza.lower()}")
        if nivel and nivel != "cualquiera":
            if nivel == "sin dato":
                f.condiciones.append("num_nivel_atencion IS NULL")
                f.descripcion.append("sin nivel de atención registrado")
            else:
                f.condiciones.append(f"num_nivel_atencion = {texto(nivel)}")
                f.descripcion.append(f"de nivel {nivel}")
        return f

    async def _seguro(self, coro) -> str:
        """Ejecuta el cuerpo de una tool y convierte las fallas en texto (contrato Tool: nunca lanzar)."""
        try:
            return await coro
        except FuenteNoDisponible as e:
            return f"Error: {e}. Puedo intentarlo de nuevo en unos segundos."
        except ConsultaInvalida:
            log.exception("Consulta inválida generada por una tool de IPS")
            return "Error: no pude armar esa consulta. Prueba formulando la pregunta de otra forma."

    # ------------------------------- tools -------------------------------

    async def describir_datos(self) -> str:
        """Describe la fuente de datos de IPS: qué contiene, cuántos registros y sedes, cobertura, calidad y fecha de corte. Úsala para preguntas generales o de resumen sobre los datos."""
        return await self._seguro(self._describir())

    async def _describir(self) -> str:
        cat, s = self.catalogo, self.catalogo.stats()
        sedes, sin_nivel, naturaleza, fechas = await asyncio.gather(
            self.datos.consultar("SELECT c_digo_sede, n_mero_sede GROUP BY c_digo_sede, n_mero_sede "
                                 "|> SELECT count(*) AS sedes"),
            self.datos.consultar("SELECT count(*) AS n WHERE num_nivel_atencion IS NULL"),
            self.datos.consultar("SELECT naturaleza, count(*) AS n GROUP BY naturaleza"),
            self.datos.consultar("SELECT fecha_corte GROUP BY fecha_corte LIMIT 3"),
        )
        total = cat.total_filas
        n_sedes = _entero(sedes[0]["sedes"]) if sedes else 0
        pct = round(100 * _entero(sin_nivel[0]["n"]) / total) if sin_nivel and total else None
        nat = ", ".join(f"{f['naturaleza']} {numero(_entero(f['n']))}" for f in
                        sorted(naturaleza, key=lambda f: -_entero(f.get("n"))) if f.get("naturaleza"))
        partes = [
            f"Relación de IPS públicas y privadas de Colombia con su capacidad instalada: {numero(total)} registros"
            + (f" de {numero(n_sedes)} sedes" if n_sedes else "")
            + f" en {s['departamentos']} departamentos y {numero(s['municipios'])} municipios.",
            "Cada registro es una sede con un tipo de capacidad y su cantidad (p. ej. camas de cuidado intensivo, "
            "salas de cirugía, ambulancias).",
            f"Grupos de capacidad: {', '.join(g.lower() for g in s['grupos_capacidad'])}; "
            f"{s['tipos_capacidad']} tipos en total.",
        ]
        if nat:
            partes.append(f"Registros por naturaleza: {nat}.")
        calidad = ["hay registros duplicados que se excluyen en las sumas",
                   "Cali y Buenaventura se cuentan en el Valle del Cauca, y Barranquilla, Cartagena y Santa Marta "
                   "en su departamento"]
        if pct is not None:
            calidad.insert(0, f"el nivel de atención no está registrado en el {pct} % de los registros")
        partes.append("Calidad de los datos: " + "; ".join(calidad) + ".")
        corte = next((c for c in (fecha_corte(f.get("fecha_corte", "")) for f in fechas) if c), None)
        partes.append(FUENTE.replace("corte noviembre de 2022", corte) if corte else FUENTE)
        return "\n".join(partes)

    async def buscar_ips(self, municipio: str = "", departamento: str = "", capacidad: str = "",
                         naturaleza: Naturaleza = "cualquiera", nivel: Nivel = "cualquiera", nombre: str = "",
                         limite: int = 5) -> str:
        """Busca sedes de IPS (hospitales, clínicas) por lugar, tipo de capacidad, naturaleza o nivel. Devuelve las de mayor capacidad con dirección, teléfono e id de sede (necesario para registrar una cita). Úsala cuando el usuario necesita dónde atenderse.

        Args:
            municipio: ciudad o municipio, tal como lo dijo el usuario (ej. "Medellín")
            departamento: departamento (ej. "Antioquia", "Valle del Cauca"); ayuda a distinguir municipios homónimos
            capacidad: servicio o capacidad buscada en palabras del usuario (ej. "cirugía", "UCI pediátrica", "ambulancias")
            naturaleza: pública, privada o mixta
            nivel: nivel de atención 1, 2 o 3 ("sin dato" si no está registrado)
            nombre: parte del nombre de la IPS si el usuario la mencionó
            limite: cuántas sedes devolver (máximo 10)
        """
        return await self._seguro(self._buscar(municipio, departamento, capacidad, naturaleza, nivel, nombre, limite))

    async def _buscar(self, municipio, departamento, capacidad, naturaleza, nivel, nombre, limite) -> str:
        f = self._filtrar(municipio, departamento, capacidad, naturaleza, nivel)
        if f.aclaracion:
            return f.aclaracion
        for palabra in _palabras_nombre(nombre):
            f.condiciones.append(f"(upper(nom_sede_ips) like {patron_sin_tildes(palabra)} OR "
                                 f"upper(nombre_prestador) like {patron_sin_tildes(palabra)})")
        if nombre.strip():
            f.descripcion.append(f"con nombre parecido a '{nombre.strip()}'")
        limite = max(1, min(int(limite or self.top), 10))
        total_q = (f"SELECT c_digo_sede, n_mero_sede {f.where()}GROUP BY c_digo_sede, n_mero_sede "
                   f"|> SELECT count(*) AS sedes")
        if capacidad.strip():
            principal = (f"SELECT {FILA} {f.where()}GROUP BY {FILA} |> SELECT {SEDE}, "
                         f"sum(num_cantidad_capacidad_instalada) AS cantidad GROUP BY {SEDE} "
                         f"ORDER BY cantidad DESC LIMIT {limite}")
        else:
            principal = f"SELECT {SEDE} {f.where()}GROUP BY {SEDE} ORDER BY nom_sede_ips LIMIT {limite}"
        total, filas = await asyncio.gather(self.datos.consultar(total_q), self.datos.consultar(principal))
        n = _entero(total[0]["sedes"]) if total else 0
        lineas = list(f.notas)
        if not n or not filas:
            return "\n".join(lineas + [f"No encontré sedes{f.de()}.", FUENTE])
        criterio = "con más capacidad" if capacidad.strip() else "en orden alfabético"
        lineas.append(f"Encontré {sedes(n)}{f.de()}."
                      + (f" Estas son las {len(filas)} {criterio}:" if n > len(filas) else ""))
        grupo_unico = next(iter(f.grupos)) if len(f.grupos) == 1 else None
        for i, s in enumerate(filas, 1):
            linea = f"{i}. {_ficha(s, self.catalogo)}"
            if capacidad.strip():
                cantidad = _entero(s.get("cantidad"))
                linea += f" · {unidad(grupo_unico, cantidad) if grupo_unico else numero(cantidad) + ' unidades'}"
            lineas.append(linea)
        lineas.append(FUENTE)
        return "\n".join(lineas)

    async def contar_capacidad(self, agrupar_por: Agrupacion = "ninguno", departamento: str = "",
                               municipio: str = "", capacidad: str = "", naturaleza: Naturaleza = "cualquiera",
                               nivel: Nivel = "cualquiera") -> str:
        """Cuenta la capacidad instalada (camas, salas, ambulancias...) sin duplicados, en total o agrupada por departamento, municipio, naturaleza, nivel o tipo. Úsala para preguntas de cuántos, totales, comparaciones o rankings.

        Args:
            agrupar_por: cómo desglosar el resultado ("ninguno" para solo el total)
            departamento: limitar a un departamento (ej. "Valle del Cauca")
            municipio: limitar a un municipio (ej. "Cali")
            capacidad: capacidad en palabras del usuario (ej. "camas de UCI", "quirófanos", "ambulancias")
            naturaleza: pública, privada o mixta
            nivel: nivel de atención 1, 2 o 3 ("sin dato" si no está registrado)
        """
        return await self._seguro(self._contar(agrupar_por, departamento, municipio, capacidad, naturaleza, nivel))

    async def _contar(self, agrupar_por, departamento, municipio, capacidad, naturaleza, nivel) -> str:
        f = self._filtrar(municipio, departamento, capacidad, naturaleza, nivel)
        if f.aclaracion:
            return f.aclaracion
        g = AGRUPACIONES.get(agrupar_por)
        cols = f"{g}, nom_grupo_capacidad" if g else "nom_grupo_capacidad"
        # 1) quitar duplicados exactos · 2) sumar por sede · 3) sumar y contar sedes por grupo
        soql = (f"SELECT {FILA} {f.where()}GROUP BY {FILA} "
                f"|> SELECT {cols}, c_digo_sede, n_mero_sede, sum(num_cantidad_capacidad_instalada) AS cantidad "
                f"GROUP BY {cols}, c_digo_sede, n_mero_sede "
                f"|> SELECT {cols}, sum(cantidad) AS cantidad, count(*) AS sedes GROUP BY {cols} "
                f"ORDER BY cantidad DESC")
        filas = await self.datos.consultar(soql, limite=2000)
        lineas = list(f.notas)
        if not filas:
            return "\n".join(lineas + [f"No encontré capacidad instalada{f.de()}.", FUENTE])

        por_etiqueta: dict[str, dict[str, list[int]]] = {}
        totales: dict[str, list[int]] = {}
        for fila in filas:
            grupo, cantidad, n_sedes = fila.get("nom_grupo_capacidad", ""), _entero(fila.get("cantidad")), \
                _entero(fila.get("sedes"))
            etiqueta = self._etiqueta(agrupar_por, fila)
            acumulado = por_etiqueta.setdefault(etiqueta, {}).setdefault(grupo, [0, 0])
            acumulado[0] += cantidad
            acumulado[1] += n_sedes
            t = totales.setdefault(grupo, [0, 0])
            t[0] += cantidad
            t[1] += n_sedes

        def resumen(grupos: dict[str, list[int]]) -> str:
            return "; ".join(f"{unidad(gr, c)} en {sedes(s)}"
                             for gr, (c, s) in sorted(grupos.items(), key=lambda x: -x[1][0]))

        lineas.append(f"Capacidad instalada{f.de()} (sin duplicados)"
                      + (f" por {agrupar_por}:" if g else ":"))
        if g:
            ordenadas = sorted(por_etiqueta.items(), key=lambda x: -sum(c for c, _ in x[1].values()))
            for etiqueta, grupos in ordenadas[:12]:
                lineas.append(f"- {etiqueta}: {resumen(grupos)}")
            if len(ordenadas) > 12:
                lineas.append(f"- … y {len(ordenadas) - 12} más")
        lineas.append(f"Total: {resumen(totales)}.")
        lineas.append(FUENTE)
        return "\n".join(lineas)

    def _etiqueta(self, agrupar_por: str, fila: dict) -> str:
        if agrupar_por == "departamento":
            return self.catalogo.nombre_departamento(fila.get("departamento", ""))
        if agrupar_por == "municipio":
            return f"{fila.get('municipio', '')} ({self.catalogo.nombre_departamento(fila.get('departamento', ''))})"
        if agrupar_por == "nivel":
            return f"Nivel {fila['num_nivel_atencion']}" if fila.get("num_nivel_atencion") else "Sin dato"
        if agrupar_por == "naturaleza":
            return fila.get("naturaleza") or "Sin dato"
        if agrupar_por == "tipo":
            return fila.get("nom_descripcion_capacidad") or "Sin dato"
        return "Total"

    async def detalle_ips(self, nombre: str, municipio: str = "") -> str:
        """Da el detalle de una IPS por su nombre: dirección, teléfono, naturaleza, nivel y toda su capacidad instalada. Acepta el nombre aproximado (sin tildes o incompleto).

        Args:
            nombre: nombre de la IPS o clínica tal como lo dijo el usuario (ej. "clínica las américas")
            municipio: municipio para distinguir sedes con nombres parecidos (opcional)
        """
        return await self._seguro(self._detalle(nombre, municipio))

    async def _detalle(self, nombre: str, municipio: str) -> str:
        palabras = _palabras_nombre(nombre)
        if not palabras:
            return "Error: indica el nombre de la IPS que quieres consultar."
        condiciones = [f"(upper(nom_sede_ips) like {patron_sin_tildes(p)} OR "
                       f"upper(nombre_prestador) like {patron_sin_tildes(p)})" for p in palabras]
        notas = []
        if municipio.strip():
            r = self.catalogo.resolver_municipio(municipio)
            if r.filtros:
                condiciones.append(r.soql())
        candidatas = await self.datos.consultar(
            f"SELECT {SEDE} WHERE {' AND '.join(condiciones)} GROUP BY {SEDE} LIMIT 50", limite=50)
        if not candidatas:
            return f"No encontré ninguna IPS con un nombre parecido a '{nombre}'." + \
                (f" (en {municipio})" if municipio.strip() else "")
        objetivo = normalizar(nombre)
        puntuadas = sorted(((fuzz.WRatio(objetivo, normalizar(f"{c.get('nom_sede_ips', '')} "
                                                               f"{c.get('nombre_prestador', '')}")), c)
                            for c in candidatas), key=lambda x: -x[0])
        mejor_puntaje, mejor = puntuadas[0]
        if mejor_puntaje < 60:
            opciones = ", ".join(c.get("nom_sede_ips", "") for _, c in puntuadas[:3])
            return f"No encontré una IPS que coincida bien con '{nombre}'. ¿Te refieres a: {opciones}?"
        parecidas = [c for p, c in puntuadas[1:6] if p >= mejor_puntaje - 3]
        if parecidas and not municipio.strip():  # varias igual de probables y sin lugar: preguntar, no adivinar
            opciones = [mejor, *parecidas][:5]
            return (f"Encontré varias IPS con un nombre parecido a '{nombre}': "
                    + "; ".join(f"{c.get('nom_sede_ips', '').strip()} ({c.get('municipio', '')})" for c in opciones)
                    + ". ¿De cuál municipio?")
        capacidades = await self.datos.consultar(
            f"SELECT nom_grupo_capacidad, nom_descripcion_capacidad, num_cantidad_capacidad_instalada "
            f"WHERE c_digo_sede = {texto(mejor.get('c_digo_sede', ''))} AND n_mero_sede = "
            f"{texto(mejor.get('n_mero_sede', ''))} "
            f"GROUP BY nom_grupo_capacidad, nom_descripcion_capacidad, num_cantidad_capacidad_instalada "
            f"|> SELECT nom_grupo_capacidad, nom_descripcion_capacidad, "
            f"sum(num_cantidad_capacidad_instalada) AS num_cantidad_capacidad_instalada "
            f"GROUP BY nom_grupo_capacidad, nom_descripcion_capacidad ORDER BY nom_grupo_capacidad")
        lineas = notas + [_ficha(mejor, self.catalogo)]
        if capacidades:
            lineas.append("Capacidad instalada:")
            por_grupo: dict[str, list[str]] = {}
            for c in capacidades:
                por_grupo.setdefault(c.get("nom_grupo_capacidad", ""), []).append(
                    f"{c.get('nom_descripcion_capacidad', '')}: {_entero(c.get('num_cantidad_capacidad_instalada'))}")
            for grupo, items in por_grupo.items():
                lineas.append(f"- {grupo.capitalize()}: {'; '.join(items)}")
        else:
            lineas.append("No tiene capacidad instalada registrada.")
        if parecidas:
            lineas.append("También hay sedes con nombre parecido: "
                          + ", ".join(f"{c.get('nom_sede_ips', '')} ({c.get('municipio', '')})" for c in parecidas))
        lineas.append(FUENTE)
        return "\n".join(lineas)

    # ------------------------------- contrato Tool -------------------------------

    def tools(self) -> list[Tool]:
        return [tool(self.describir_datos), tool(self.buscar_ips), tool(self.contar_capacidad),
                tool(self.detalle_ips)]


def _lista(valores: list[str], maximo: int = 4) -> str:
    if len(valores) <= maximo:
        return " o ".join(", ".join(valores).rsplit(", ", 1)) if len(valores) > 1 else valores[0]
    return f"{', '.join(valores[:maximo])} y {len(valores) - maximo} tipos más"


def _palabras_nombre(nombre: str) -> list[str]:
    return [p for p in normalizar(nombre).split() if len(p) >= 3 and p not in _PALABRAS_VACIAS]


def _ficha(s: dict, catalogo: Catalogo) -> str:
    """Una sede en una línea, lista para voz y para registrar una cita (incluye el id de sede)."""
    nombre = s.get("nom_sede_ips", "").strip()
    prestador = s.get("nombre_prestador", "").strip()
    partes = [nombre + (f" ({prestador})" if prestador and normalizar(prestador) != normalizar(nombre) else "")]
    partes.append(f"{s.get('municipio', '')}, {catalogo.nombre_departamento(s.get('departamento', ''))}")
    nivel = s.get("num_nivel_atencion")
    partes.append(f"{s.get('naturaleza', 'naturaleza sin dato')}, {'nivel ' + nivel if nivel else 'nivel sin dato'}")
    if s.get("direcci_n"):
        partes.append(s["direcci_n"].strip())
    if s.get("tel_fono"):
        partes.append(f"tel {s['tel_fono'].strip()}")
    partes.append(f"id {s.get('c_digo_sede', '')}-{s.get('n_mero_sede', '')}")
    return " · ".join(partes)
