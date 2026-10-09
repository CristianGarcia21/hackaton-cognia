"""Brief de la fuente de datos (spec §9.5, issue #12): paso P3 del guion, «de qué trata la fuente» + 3 a 5 preguntas.

    b = await generar(datos, catalogo)   # -> Resultado(brief: ev.Brief, saludo: str)

1. Estadísticas REALES de datos.gov.co (consultas en paralelo; sumas sin duplicados con el mismo SoQL que las
   tools de IPS, así las cifras del brief coinciden con lo que dice el agente).
2. El LLM redacta resumen, puntos clave, preguntas sugeridas y un saludo hablado de 2 frases, usando SOLO
   esas cifras. Si el LLM falla o tarda, se arma un brief determinista con las mismas estadísticas: el panel
   nunca queda vacío en la demo.
Se calcula una vez al arrancar (server/main.py lo guarda en caché y lo envía a cada sesión).
"""

import asyncio
import logging
from dataclasses import dataclass

from pydantic import BaseModel, Field

from core import llm
from server import events as ev
from server.data import datos_gov
from server.tools.ips import HerramientasIPS, _entero, _Filtros, fecha_corte, numero, unidad

log = logging.getLogger("cognia.brief")

MODELO = "groq/openai/gpt-oss-120b"  # una sola llamada al arrancar: vale la pena el modelo grande
TIMEOUT_S = 20.0
TOP_DEPARTAMENTOS = 5

# Preguntas que las tools responden con certeza: completan las del LLM si trae menos de 3.
PREGUNTAS_RESPALDO = [
    "¿Cuántas camas de cuidado intensivo hay en Antioquia?",
    "¿Qué IPS públicas tienen sala de cirugía en Cali?",
    "¿Cómo se distribuyen las ambulancias por departamento?",
    "Necesito una cita para una cirugía en Medellín",
]

SISTEMA = (
    "Eres el analista de datos de Kognia, un agente de voz que orienta sobre IPS (hospitales y clínicas) de "
    "Colombia. Con las ESTADÍSTICAS REALES que te doy, escribe un brief en español para el panel de la demo.\n"
    "Reglas:\n"
    "- Usa SOLO las cifras de las estadísticas; no inventes ni redondees de forma engañosa. Si un dato falta, "
    "no lo menciones.\n"
    "- resumen: 1 o 2 frases de qué es la fuente y para qué sirve.\n"
    "- puntos_clave: 3 a 5 hallazgos concretos con cifras (incluye una limitación de calidad de los datos).\n"
    "- preguntas_sugeridas: 3 a 5 preguntas cortas que un usuario haría por voz y que se responden con estos "
    "datos (conteos por departamento o municipio, IPS con cierta capacidad en una ciudad, detalle de una IPS, "
    "pedir una cita en una sede). Nada sobre horarios, precios ni disponibilidad: no están en los datos.\n"
    "- saludo: exactamente 2 frases para decir en voz alta al empezar: preséntate como Kognia y di qué puedes "
    "hacer. Sin cifras largas, sin listas ni markdown."
)


class BriefLLM(BaseModel):
    resumen: str
    puntos_clave: list[str] = Field(min_length=1, max_length=6)
    preguntas_sugeridas: list[str] = Field(min_length=1, max_length=6)
    saludo: str


@dataclass(frozen=True)
class Resultado:
    brief: ev.Brief
    saludo: str
    con_llm: bool


async def estadisticas(datos, catalogo: datos_gov.Catalogo) -> dict:
    """Cifras reales de la fuente (6 consultas en paralelo). Lanza FuenteNoDisponible si la API no responde."""
    h = HerramientasIPS(datos, catalogo)
    sin_filtros = _Filtros()
    sedes, sin_nivel, naturaleza, fechas, por_depto, por_grupo = await asyncio.gather(
        datos.consultar("SELECT c_digo_sede, n_mero_sede GROUP BY c_digo_sede, n_mero_sede "
                        "|> SELECT count(*) AS sedes"),
        datos.consultar("SELECT count(*) AS n WHERE num_nivel_atencion IS NULL"),
        datos.consultar("SELECT naturaleza, count(*) AS n GROUP BY naturaleza"),
        datos.consultar("SELECT fecha_corte GROUP BY fecha_corte LIMIT 3"),
        datos.consultar(h._suma_por(sin_filtros, "departamento"), limite=100),
        datos.consultar(h._suma_por(sin_filtros, "nom_grupo_capacidad"), limite=50),
    )
    s = catalogo.stats()
    total = catalogo.total_filas

    # Sedes por departamento real (Cali y Buenaventura suman al Valle, etc., como en las tools).
    deptos: dict[str, int] = {}
    for f in por_depto:
        nombre = catalogo.nombre_departamento(f.get("departamento", ""))
        deptos[nombre] = deptos.get(nombre, 0) + _entero(f.get("sedes"))
    top = sorted(deptos.items(), key=lambda kv: -kv[1])[:TOP_DEPARTAMENTOS]

    grupos = {f["nom_grupo_capacidad"]: _entero(f.get("cantidad")) for f in por_grupo if f.get("nom_grupo_capacidad")}
    corte = next((c for c in (fecha_corte(f.get("fecha_corte", "")) for f in fechas) if c), None)
    return {
        "registros": total,
        "sedes": _entero(sedes[0].get("sedes")) if sedes else None,
        "departamentos": s["departamentos"],
        "municipios": s["municipios"],
        "tipos_capacidad": s["tipos_capacidad"],
        "sin_nivel_pct": round(100 * _entero(sin_nivel[0].get("n")) / total) if sin_nivel and total else None,
        "por_naturaleza": {f["naturaleza"]: _entero(f.get("n")) for f in naturaleza if f.get("naturaleza")},
        "top_departamentos_por_sedes": dict(top),
        "capacidad_por_grupo": grupos,
        "fecha_corte": corte,
    }


def _limpio(textos: list[str], maximo: int) -> list[str]:
    vistos, salida = set(), []
    for t in (" ".join(str(x).split()) for x in textos):
        if t and t.lower() not in vistos:
            vistos.add(t.lower())
            salida.append(t)
    return salida[:maximo]


def _preguntas(del_llm: list[str]) -> list[str]:
    """3 a 5 preguntas: las del LLM y, si faltan, las de respaldo (que las tools responden con certeza)."""
    return _limpio(del_llm + PREGUNTAS_RESPALDO, 5) if len(_limpio(del_llm, 5)) < 3 else _limpio(del_llm, 5)


def respaldo(stats: dict) -> Resultado:
    """Brief sin LLM, con las mismas cifras reales (si Groq falla al arrancar)."""
    sedes = f" de {numero(stats['sedes'])} sedes" if stats.get("sedes") else ""
    corte = f" (corte {stats['fecha_corte']})" if stats.get("fecha_corte") else ""
    resumen = (f"Relación de IPS públicas y privadas de Colombia con su capacidad instalada: "
               f"{numero(stats['registros'])} registros{sedes} en {stats['departamentos']} departamentos{corte}.")
    puntos = []
    if stats.get("top_departamentos_por_sedes"):
        top = ", ".join(f"{d} ({numero(n)})" for d, n in list(stats["top_departamentos_por_sedes"].items())[:3])
        puntos.append(f"Departamentos con más sedes: {top}.")
    if stats.get("capacidad_por_grupo"):
        puntos.append("Capacidad instalada: " + ", ".join(
            f"{numero(n)} {unidad(g, n)}" for g, n in list(stats["capacidad_por_grupo"].items())[:4]) + ".")
    if stats.get("por_naturaleza"):
        puntos.append("Registros por naturaleza: " + ", ".join(
            f"{k.lower()} {numero(v)}" for k, v in stats["por_naturaleza"].items()) + ".")
    if stats.get("sin_nivel_pct") is not None:
        puntos.append(f"El nivel de atención no está registrado en el {stats['sin_nivel_pct']} % de los registros.")
    saludo = ("Hola, soy Kognia. Te ayudo a encontrar IPS en Colombia, consultar su capacidad instalada y "
              "registrar una solicitud de cita.")
    brief = ev.Brief(summary=resumen, key_points=puntos or [resumen], questions=PREGUNTAS_RESPALDO,
                     stats=_stats_panel(stats))
    return Resultado(brief, saludo, con_llm=False)


def _stats_panel(stats: dict) -> dict:
    """Cifras planas para mostrar como chips en el panel (las anidadas quedan en el brief del LLM)."""
    return {k: stats[k] for k in ("registros", "sedes", "departamentos", "municipios", "sin_nivel_pct")
            if stats.get(k) is not None}


async def generar(datos, catalogo: datos_gov.Catalogo) -> Resultado:
    """Estadísticas reales → LLM (o respaldo). Lanza FuenteNoDisponible solo si la API no da las cifras."""
    stats = await estadisticas(datos, catalogo)
    try:
        r = await llm.astructured(f"ESTADÍSTICAS REALES (JSON):\n{stats}", BriefLLM, model=MODELO, system=SISTEMA,
                                  retries=1, total_timeout=TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — sin LLM el brief sale igual, con las cifras
        log.warning("Brief sin LLM (%s: %s); uso el respaldo determinista", type(e).__name__, str(e)[:150])
        return respaldo(stats)
    puntos = _limpio(r.puntos_clave, 5) or respaldo(stats).brief.key_points
    brief = ev.Brief(summary=" ".join(r.resumen.split()), key_points=puntos, questions=_preguntas(r.preguntas_sugeridas),
                     stats=_stats_panel(stats))
    saludo = " ".join(r.saludo.split()) or respaldo(stats).saludo
    return Resultado(brief, saludo, con_llm=True)
