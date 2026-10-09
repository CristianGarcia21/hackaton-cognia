"""Tools de IPS sobre la API de datos.gov.co (spec §8). Contrato de Kognia: core.tools.Tool (@tool).

    h = HerramientasIPS(cliente_datos_gov, catalogo)
    tools = h.tools()            # para el Voice Agent / el hub MCP (mcp_servers/ips.py)

Reglas (spec §8, decisión A): el LLM NUNCA escribe SoQL; estas tools lo arman con los filtros exactos que
da el resolvedor del catálogo. Resultados compactos para voz, errores como texto (nunca excepciones),
sumas SIN duplicados (pipe |>), totales de una consulta propia (nunca de un desglose truncado) y unidades
separadas por grupo (camas, salas...: no se suman entre sí). Si lo dicho es ambiguo (homónimos, nombre
genérico) o no se reconoce, la tool NO adivina: devuelve la aclaración.

Las tools son ASYNC: quien las invoque (hub MCP, Voice Agent) debe hacer `await tool(**args)`. El `Agent`
síncrono de core/ no las espera; no registrarlas ahí.
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
NATURALEZAS = {"publica": "Pública", "privada": "Privada", "mixta": "Mixta"}
LIMITE_DESGLOSE = 400       # filas del desglose (ordenado por cantidad); el total sale de otra consulta
MAX_ETIQUETAS = 12          # líneas del desglose que se leen por voz
MAX_ITEMS_POR_GRUPO = 5     # tipos por grupo en el detalle de una sede
MARGEN_EMPATE = 10         # puntos de parecido dentro de los cuales dos sedes se consideran empatadas
_PALABRAS_VACIAS = {"de", "del", "la", "las", "el", "los", "y", "sede", "ips", "sas", "s", "a", "e", "en"}
_GENERICAS = {"clinica", "clinicas", "hospital", "hospitales", "centro", "centros", "ese", "medico", "medica",
              "medicos", "salud", "unidad", "fundacion", "empresa", "social", "estado", "servicios", "servicio",
              "ltda", "sociedad", "consultorio", "laboratorio", "odontologia", "odontologico", "nacional"}

Naturaleza = Literal["cualquiera", "Pública", "Privada", "Mixta"]
Nivel = Literal["cualquiera", "1", "2", "3", "sin dato"]
Agrupacion = Literal["ninguno", "departamento", "municipio", "naturaleza", "nivel", "tipo"]


class EntradaInvalida(ValueError):
    """Un parámetro que el LLM mandó fuera de los valores válidos: se le explica, no se consulta."""


def numero(n: int) -> str:
    """1103 → '1.103' (separador de miles colombiano)."""
    return f"{n:,}".replace(",", ".")


def _entero(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _txt(v) -> str:
    return "" if v is None else str(v).strip()


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


def canon_naturaleza(valor) -> str | None:
    """'publica', 'PÚBLICA', 'Pública' → 'Pública'; 'cualquiera'/'' → None. Otro valor → EntradaInvalida."""
    v = normalizar(valor)
    if v in ("", "cualquiera", "todas", "cualquier"):
        return None
    for clave, canon in NATURALEZAS.items():
        if v.startswith(clave[:5]):
            return canon
    raise EntradaInvalida(f"naturaleza '{valor}' no es válida: usa Pública, Privada o Mixta")


def canon_nivel(valor) -> str | None:
    """'2', 'nivel 2', 2 → '2'; 'sin dato' → 'sin dato'; 'cualquiera'/'' → None. Otro valor → EntradaInvalida."""
    v = normalizar(valor)
    if v in ("", "cualquiera", "todos", "cualquier", "0"):
        return None
    if "sin" in v or "dato" in v or "no registrado" in v:
        return "sin dato"
    niveles = set(re.findall(r"\b([123])\b", v))
    if len(niveles) > 1:
        raise EntradaInvalida(f"nivel '{valor}' tiene varios valores: consulta un nivel a la vez")
    if niveles:
        return niveles.pop()
    raise EntradaInvalida(f"nivel '{valor}' no es válido: usa 1, 2, 3 o 'sin dato'")


@dataclass
class _Filtros:
    condiciones: list[str] = field(default_factory=list)
    descripcion: list[str] = field(default_factory=list)  # "en MEDELLÍN (Antioquia)", "con «cirugía»"
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

    def _filtrar(self, municipio="", departamento="", capacidad="", naturaleza="cualquiera",
                 nivel="cualquiera") -> _Filtros:
        municipio, departamento, capacidad = _txt(municipio), _txt(departamento), _txt(capacidad)
        f, cat = _Filtros(), self.catalogo
        if municipio:
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
        elif departamento:
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
        if capacidad:
            r = cat.resolver_capacidad(capacidad)
            if not r.filtros:
                utiles = [s for s in r.sugerencias if fuzz.WRatio(normalizar(capacidad), normalizar(s)) >= 70]
                grupos = ", ".join(g.lower() for g in cat.stats()["grupos_capacidad"])
                f.aclaracion = (f"No reconozco la capacidad '{capacidad}' en estos datos"
                                + (f". ¿Te refieres a: {', '.join(utiles)}?" if utiles else
                                   f": solo registran capacidad instalada de {grupos}."))
                return f
            if not r.exacto:
                f.notas.append(f"Interpreté '{capacidad}' como {_lista(r.valores)}.")
            f.condiciones.append(r.soql())
            f.grupos = {x["nom_grupo_capacidad"] for x in r.filtros}
            # Para voz: lo que dijo el usuario, no la lista de 10 tipos técnicos.
            f.descripcion.append(f"con {_lista(r.valores) if len(r.valores) <= 2 else '«' + capacidad + '»'}")
        nat = canon_naturaleza(naturaleza)
        if nat:
            f.condiciones.append(f"naturaleza = {texto(nat)}")
            f.descripcion.append(f"de naturaleza {nat.lower()}")
        niv = canon_nivel(nivel)
        if niv == "sin dato":
            f.condiciones.append("num_nivel_atencion IS NULL")
            f.descripcion.append("sin nivel de atención registrado")
        elif niv:
            f.condiciones.append(f"num_nivel_atencion = {texto(niv)}")
            f.descripcion.append(f"de nivel {niv}")
        return f

    async def _seguro(self, coro) -> str:
        """Ejecuta el cuerpo de una tool y convierte TODA falla en texto (contrato Tool: nunca lanzar).
        CancelledError no hereda de Exception: una interrupción de voz se propaga normalmente."""
        if self.catalogo is None:  # el servidor arrancó pero datos.gov.co aún no entrega el catálogo
            coro.close()
            return "Error: los datos de datos.gov.co se están cargando todavía. Intenta en unos segundos."
        try:
            return await coro
        except FuenteNoDisponible as e:
            return f"Error: {e}. Puedo intentarlo de nuevo en unos segundos."
        except EntradaInvalida as e:
            return f"Error: {e}."
        except ConsultaInvalida:
            log.exception("Consulta inválida generada por una tool de IPS")
            return "Error: no pude armar esa consulta. Prueba formulando la pregunta de otra forma."
        except Exception:  # noqa: BLE001 — un bug no debe tumbar el turno de voz
            log.exception("Error inesperado en una tool de IPS")
            return "Error: tuve un problema consultando los datos. Prueba de nuevo o reformula la pregunta."

    # ------------------------------- tools -------------------------------

    async def describir_datos(self) -> str:
        """Describe la fuente de datos de IPS: qué contiene, cuántos registros y sedes, cobertura, calidad y fecha de corte. Úsala para preguntas generales o de resumen sobre los datos."""
        return await self._seguro(self._describir())

    async def _describir(self) -> str:
        cat, s = self.catalogo, self.catalogo.stats()
        n_sedes, sin_nivel, naturaleza, fechas = await asyncio.gather(
            self.datos.consultar("SELECT c_digo_sede, n_mero_sede GROUP BY c_digo_sede, n_mero_sede "
                                 "|> SELECT count(*) AS sedes"),
            self.datos.consultar("SELECT count(*) AS n WHERE num_nivel_atencion IS NULL"),
            self.datos.consultar("SELECT naturaleza, count(*) AS n GROUP BY naturaleza"),
            self.datos.consultar("SELECT fecha_corte GROUP BY fecha_corte LIMIT 3"),
        )
        total = cat.total_filas
        cuantas = _entero(n_sedes[0].get("sedes")) if n_sedes else 0
        pct = round(100 * _entero(sin_nivel[0].get("n")) / total) if sin_nivel and total else None
        nat = ", ".join(f"{f['naturaleza']} {numero(_entero(f.get('n')))}" for f in
                        sorted(naturaleza, key=lambda f: -_entero(f.get("n"))) if f.get("naturaleza"))
        partes = [
            f"Relación de IPS públicas y privadas de Colombia con su capacidad instalada: {numero(total)} registros"
            + (f" de {sedes(cuantas)}" if cuantas else "")
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
        """Busca sedes de IPS (hospitales, clínicas) por lugar, tipo de capacidad, naturaleza o nivel. Devuelve las de mayor capacidad instalada con dirección, teléfono e id de sede (necesario para registrar una cita). Úsala cuando el usuario necesita dónde atenderse.

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
        palabras = _palabras_nombre(nombre)
        for palabra in palabras:
            f.condiciones.append(f"(upper(nom_sede_ips) like {patron_sin_tildes(palabra)} OR "
                                 f"upper(nombre_prestador) like {patron_sin_tildes(palabra)})")
        if palabras:
            f.descripcion.append(f"con nombre parecido a '{_txt(nombre)}'")
        limite = max(1, min(_entero(limite) or self.top, 10))
        total_q = (f"SELECT c_digo_sede, n_mero_sede {f.where()}GROUP BY c_digo_sede, n_mero_sede "
                   f"|> SELECT count(*) AS sedes")
        # Siempre por capacidad instalada (sin duplicados): con capacidad, la pedida; sin ella, la total.
        principal = (f"SELECT {FILA} {f.where()}GROUP BY {FILA} |> SELECT {SEDE}, "
                     f"sum(num_cantidad_capacidad_instalada) AS cantidad GROUP BY {SEDE} "
                     f"ORDER BY cantidad DESC, nom_sede_ips LIMIT {limite}")
        total, filas = await asyncio.gather(self.datos.consultar(total_q), self.datos.consultar(principal))
        n = _entero(total[0].get("sedes")) if total else 0
        lineas = list(f.notas)
        if not n or not filas:
            return "\n".join(lineas + [f"No encontré sedes{f.de()}.", FUENTE])
        lineas.append(f"Encontré {sedes(n)}{f.de()}."
                      + (f" Estas son las {len(filas)} con más capacidad instalada:" if n > len(filas) else ""))
        grupo_unico = next(iter(f.grupos)) if len(f.grupos) == 1 else None
        for i, s in enumerate(filas, 1):
            linea = f"{i}. {_ficha(s, self.catalogo)}"
            if _txt(capacidad):
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

    def _suma_por(self, f: _Filtros, cols: str, limite: str = "") -> str:
        """1) quitar duplicados exactos · 2) sumar por sede · 3) sumar y contar sedes por `cols`."""
        return (f"SELECT {FILA} {f.where()}GROUP BY {FILA} "
                f"|> SELECT {cols}, c_digo_sede, n_mero_sede, sum(num_cantidad_capacidad_instalada) AS cantidad "
                f"GROUP BY {cols}, c_digo_sede, n_mero_sede "
                f"|> SELECT {cols}, sum(cantidad) AS cantidad, count(*) AS sedes GROUP BY {cols} "
                f"ORDER BY cantidad DESC{limite}")

    async def _contar(self, agrupar_por, departamento, municipio, capacidad, naturaleza, nivel) -> str:
        f = self._filtrar(municipio, departamento, capacidad, naturaleza, nivel)
        if f.aclaracion:
            return f.aclaracion
        agrupar_por = normalizar(agrupar_por) or "ninguno"
        g = AGRUPACIONES.get(agrupar_por)
        if agrupar_por != "ninguno" and not g:
            raise EntradaInvalida(f"agrupar_por '{agrupar_por}' no es válido: usa ninguno, "
                                  + ", ".join(AGRUPACIONES))
        # El TOTAL sale de su propia consulta (nunca truncada): una sede cuenta una sola vez por grupo.
        consultas = [self.datos.consultar(self._suma_por(f, "nom_grupo_capacidad"))]
        if g:
            consultas.append(self.datos.consultar(
                self._suma_por(f, f"{g}, nom_grupo_capacidad", f" LIMIT {LIMITE_DESGLOSE}"),
                limite=LIMITE_DESGLOSE))
        resultados = await asyncio.gather(*consultas)
        totales = {t.get("nom_grupo_capacidad", ""): (_entero(t.get("cantidad")), _entero(t.get("sedes")))
                   for t in resultados[0]}
        lineas = list(f.notas)
        if not totales:
            return "\n".join(lineas + [f"No encontré capacidad instalada{f.de()}.", FUENTE])

        def resumen(grupos: dict[str, tuple[int, int]], maximo: int = 7) -> str:
            ordenados = sorted(grupos.items(), key=lambda x: -x[1][0])
            texto_ = "; ".join(f"{unidad(gr, c)} en {sedes(s)}" for gr, (c, s) in ordenados[:maximo])
            return texto_ + (f"; y {len(ordenados) - maximo} grupos más" if len(ordenados) > maximo else "")

        lineas.append(f"Capacidad instalada{f.de()} (sin duplicados)" + (f" por {agrupar_por}:" if g else ":"))
        if g:
            desglose = resultados[1]
            por_etiqueta: dict[str, dict[str, list[int]]] = {}
            for fila in desglose:
                acumulado = por_etiqueta.setdefault(self._etiqueta(agrupar_por, fila), {}).setdefault(
                    fila.get("nom_grupo_capacidad", ""), [0, 0])
                acumulado[0] += _entero(fila.get("cantidad"))
                acumulado[1] += _entero(fila.get("sedes"))
            ordenadas = sorted(por_etiqueta.items(), key=lambda x: -sum(c for c, _ in x[1].values()))
            # sin capacidad pedida hay hasta 7 grupos por línea: se leen los 3 principales
            por_linea = 7 if _txt(capacidad) else 3
            for etiqueta, grupos in ordenadas[:MAX_ETIQUETAS]:
                lineas.append(f"- {etiqueta}: {resumen({k: tuple(v) for k, v in grupos.items()}, por_linea)}")
            if len(desglose) >= LIMITE_DESGLOSE:
                lineas.append("- … y otros con menos capacidad")
            elif len(ordenadas) > MAX_ETIQUETAS:
                lineas.append(f"- … y {len(ordenadas) - MAX_ETIQUETAS} más")
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
        """Da el detalle de una IPS por su nombre: dirección, teléfono, naturaleza, nivel y su capacidad instalada. Acepta el nombre aproximado (sin tildes o incompleto); si hay varias sedes parecidas, pregunta cuál.

        Args:
            nombre: nombre de la IPS o clínica tal como lo dijo el usuario (ej. "clínica las américas")
            municipio: municipio para distinguir sedes con nombres parecidos (opcional)
        """
        return await self._seguro(self._detalle(nombre, municipio))

    async def _detalle(self, nombre, municipio) -> str:
        nombre, municipio = _txt(nombre), _txt(municipio)
        palabras = _palabras_nombre(nombre)
        if not palabras:
            return "Error: indica el nombre de la IPS que quieres consultar."
        distintivas = [p for p in palabras if p not in _GENERICAS]
        if not distintivas:
            return (f"'{nombre}' es muy general (hay miles de sedes así). ¿Cómo se llama la IPS o en qué "
                    f"municipio está?" if not municipio else
                    f"Hay muchas sedes que se llaman '{nombre}' en {municipio}. ¿Recuerdas el nombre completo?")
        condiciones = [f"(upper(nom_sede_ips) like {patron_sin_tildes(p)} OR "
                       f"upper(nombre_prestador) like {patron_sin_tildes(p)})" for p in palabras]
        notas = []
        if municipio:
            r = self.catalogo.resolver_municipio(municipio)
            if not r.filtros:
                return (f"No encontré el municipio '{municipio}'"
                        + (f". ¿Quisiste decir: {', '.join(r.sugerencias)}?" if r.sugerencias else "."))
            if not r.exacto:
                lugares = sorted({f"{x['municipio']} ({self.catalogo.nombre_departamento(x['departamento'])})"
                                  for x in r.filtros})
                if len(lugares) > 1:
                    return f"Hay varios municipios que coinciden con '{municipio}': {', '.join(lugares)}. ¿Cuál?"
                notas.append(f"Interpreté '{municipio}' como {lugares[0]}.")
            condiciones.append(r.soql())
        candidatas = await self.datos.consultar(
            f"SELECT {SEDE} WHERE {' AND '.join(condiciones)} GROUP BY {SEDE} ORDER BY nom_sede_ips LIMIT 50",
            limite=50)
        if not candidatas:
            return "\n".join(notas + [f"No encontré ninguna IPS con un nombre parecido a '{nombre}'"
                                      + (f" en {municipio}." if municipio else ".")])
        objetivo = normalizar(nombre)

        def puntaje(c: dict) -> tuple:
            texto_c = normalizar(f"{c.get('nom_sede_ips', '')} {c.get('nombre_prestador', '')}")
            # 1) que contenga TODAS las palabras distintivas · 2) parecido · 3) nombre más corto (menos ruido)
            return (all(p in texto_c for p in distintivas), fuzz.WRatio(objetivo, texto_c), -len(texto_c))

        puntuadas = sorted(((puntaje(c), c) for c in candidatas), key=lambda x: x[0], reverse=True)
        (completo, mejor_puntaje, _), mejor = puntuadas[0]
        if mejor_puntaje < 60:
            opciones = "; ".join(_nombre_corto(c) for _, c in puntuadas[:3])
            return "\n".join(notas + [f"No encontré una IPS que coincida bien con '{nombre}'. ¿Te refieres a: {opciones}?"])
        # Una sola sede cuyo nombre tiene EXACTAMENTE las palabras distintivas dichas ("pablo tobón uribe" →
        # HOSPITAL PABLO TOBON URIBE y no "... SEDE BELÉN"): es la principal; las demás se mencionan.
        exactas = [c for _, c in puntuadas if _distintivas(c.get("nom_sede_ips")) == set(distintivas)]
        if len(exactas) == 1:
            mejor = exactas[0]
            otras = [c for _, c in puntuadas if c is not mejor][:3]
            return await self._ficha_detalle(mejor, notas, otras)
        # Varias con todas las palabras distintivas y parecido cercano: preguntar, no adivinar (un margen
        # amplio evita que gane un nombre largo que solo CONTIENE la frase, p. ej. una unidad renal).
        empatadas = [c for (comp, p, _), c in puntuadas[1:8] if comp == completo and p >= mejor_puntaje - MARGEN_EMPATE]
        if empatadas:
            vistos, opciones = set(), []
            for c in [mejor, *empatadas]:
                clave = (normalizar(c.get('nom_sede_ips')), c.get('municipio'))
                if clave not in vistos:
                    vistos.add(clave)
                    opciones.append(c)
            opciones = opciones[:5]
            mismo_lugar = len({c.get("municipio") for c in opciones}) == 1
            return "\n".join(notas + [
                f"Encontré varias IPS con un nombre parecido a '{nombre}'"
                + (f" en {opciones[0].get('municipio', '')}: " if mismo_lugar else ": ")
                + "; ".join(_nombre_corto(c) + ("" if mismo_lugar else f" ({c.get('municipio', '')})")
                            for c in opciones)
                + (". ¿Cuál sede?" if mismo_lugar else ". ¿De cuál municipio o cuál sede?")])
        return await self._ficha_detalle(mejor, notas, [])

    async def _ficha_detalle(self, mejor: dict, notas: list[str], otras: list[dict]) -> str:
        capacidades = await self.datos.consultar(
            f"SELECT nom_grupo_capacidad, nom_descripcion_capacidad, num_cantidad_capacidad_instalada "
            f"WHERE c_digo_sede = {texto(mejor.get('c_digo_sede', ''))} AND n_mero_sede = "
            f"{texto(mejor.get('n_mero_sede', ''))} "
            f"GROUP BY nom_grupo_capacidad, nom_descripcion_capacidad, num_cantidad_capacidad_instalada "
            f"|> SELECT nom_grupo_capacidad, nom_descripcion_capacidad, "
            f"sum(num_cantidad_capacidad_instalada) AS num_cantidad_capacidad_instalada "
            f"GROUP BY nom_grupo_capacidad, nom_descripcion_capacidad "
            f"ORDER BY nom_grupo_capacidad, num_cantidad_capacidad_instalada DESC")
        lineas = notas + [_ficha(mejor, self.catalogo)]
        if capacidades:
            lineas.append("Capacidad instalada:")
            por_grupo: dict[str, list[tuple[str, int]]] = {}
            for c in capacidades:
                por_grupo.setdefault(c.get("nom_grupo_capacidad", ""), []).append(
                    (c.get("nom_descripcion_capacidad", ""), _entero(c.get("num_cantidad_capacidad_instalada"))))
            for grupo, items in por_grupo.items():
                items.sort(key=lambda x: -x[1])
                total_grupo = sum(n for _, n in items)
                visibles = "; ".join(f"{t}: {n}" for t, n in items[:MAX_ITEMS_POR_GRUPO])
                resto = f"; y {len(items) - MAX_ITEMS_POR_GRUPO} tipos más" if len(items) > MAX_ITEMS_POR_GRUPO else ""
                lineas.append(f"- {unidad(grupo, total_grupo)} ({visibles}{resto})")
        else:
            lineas.append("No tiene capacidad instalada registrada.")
        if otras:
            lineas.append("También hay: " + "; ".join(f"{_nombre_corto(c)} ({c.get('municipio', '')})" for c in otras))
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


def _palabras_nombre(nombre) -> list[str]:
    return [p for p in normalizar(_txt(nombre)).split() if len(p) >= 3 and p not in _PALABRAS_VACIAS]


def _distintivas(nombre) -> set[str]:
    return {p for p in _palabras_nombre(nombre) if p not in _GENERICAS}


def _nombre_corto(s: dict) -> str:
    return _txt(s.get("nom_sede_ips"))


def _ficha(s: dict, catalogo: Catalogo) -> str:
    """Una sede en una línea, lista para voz y para registrar una cita (incluye el id de sede)."""
    nombre, prestador = _txt(s.get("nom_sede_ips")), _txt(s.get("nombre_prestador"))
    n_nombre, n_prestador = normalizar(nombre), normalizar(prestador)
    # El prestador solo si aporta (no si repite el nombre de la sede) y recortado para voz.
    aporta = prestador and n_prestador not in n_nombre and n_nombre not in n_prestador
    partes = [nombre + (f" ({prestador[:60] + ('…' if len(prestador) > 60 else '')})" if aporta else "")]
    partes.append(f"{s.get('municipio', '')}, {catalogo.nombre_departamento(s.get('departamento', ''))}")
    nivel = s.get("num_nivel_atencion")
    partes.append(f"{s.get('naturaleza', 'naturaleza sin dato')}, {'nivel ' + nivel if nivel else 'nivel sin dato'}")
    if s.get("direcci_n"):
        partes.append(_txt(s["direcci_n"]))
    if s.get("tel_fono"):
        partes.append(f"tel {_txt(s['tel_fono'])}")
    partes.append(f"id {s.get('c_digo_sede', '')}-{s.get('n_mero_sede', '')}")
    return " · ".join(partes)
