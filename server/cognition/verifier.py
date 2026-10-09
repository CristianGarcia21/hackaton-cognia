"""Verificador QA en vivo (spec §9.4): ¿lo que dijo el agente está respaldado por lo que devolvieron las tools?

    v = await verificar(Turno(pregunta="...", tools=[ResultadoTool("buscar_ips", {...}, "...")], respuesta="..."))
    v.estado  # respaldado | parcial | no_respaldado | fuera_de_datos_ok

Corre en paralelo cuando el agente termina de hablar; nunca bloquea la voz. Sin tools y sin cifras
(repreguntas, saludos, "llama al 123") se decide sin LLM: no gasta cuota en cada turno.
"""

import json
import logging
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, field_validator

from core import llm
from server.events import EstadoVerificacion

log = logging.getLogger("cognia.verifier")

MODELO = "groq/openai/gpt-oss-120b"  # comparar cifras y nombres pide más razonamiento que las emociones
TIMEOUT_S = 8.0
MAX_RESULTADO = 2500  # caracteres por resultado de tool que ve el verificador

_DIGITO = re.compile(r"\d")

SISTEMA = """Eres un verificador de hechos de un asistente de salud por voz. Comparas la RESPUESTA del asistente con los RESULTADOS DE TOOLS (consultas a datos.gov.co, la única fuente válida).
HECHOS DEL SISTEMA (válidos aunque no aparezcan en los resultados; el asistente los conoce por su prompt): el registro es el REPS de IPS de datos.gov.co con corte del 5 de noviembre de 2022; no incluye horarios, especialistas, precios, EPS ni disponibilidad; la línea de emergencias en Colombia es el 123; las citas quedan como solicitud pendiente de confirmación por la IPS.
Reglas:
- Cada cifra, nombre de IPS, dirección, teléfono, municipio o atributo que afirme la respuesta debe aparecer en los resultados (los números pueden estar escritos en palabras: "veintitrés" = 23). Redondeos razonables están bien.
- "respaldado": todo lo afirmado está en los resultados.
- "parcial": todo dato concreto está en los resultados, pero hay una imprecisión menor de redacción (p. ej. un redondeo dudoso).
- "no_respaldado": aunque el resto sea correcto, hay al menos UNA cifra, nombre de IPS, dirección o teléfono que no aparece en los resultados o los contradice, o la respuesta da datos concretos sin haber consultado. Un dato inventado nunca es "parcial".
- "fuera_de_datos_ok": la respuesta no afirma datos de IPS (pide aclaración, dice que algo no está en los datos, orienta a llamar al 123, saluda).
Devuelve solo JSON: {"estado": ..., "problemas": [frases cortas en español, máximo 3], "correccion": frase corta para decir en voz con el dato correcto según los resultados, o null}.
Solo propone correccion si estado es "no_respaldado" y los resultados permiten corregir."""


@dataclass(frozen=True)
class ResultadoTool:
    nombre: str
    args: dict
    resultado: str


@dataclass
class Turno:
    pregunta: str = ""
    tools: list[ResultadoTool] = field(default_factory=list)
    respuesta: str = ""


class Veredicto(BaseModel):
    estado: EstadoVerificacion = "parcial"
    problemas: list[str] = Field(default_factory=list)
    correccion: str | None = None

    @field_validator("estado", mode="before")
    @classmethod
    def _estado(cls, v):
        k = str(v or "").strip().lower().replace(" ", "_").replace("-", "_")
        return k if k in ("respaldado", "parcial", "no_respaldado", "fuera_de_datos_ok") else "parcial"

    @field_validator("problemas", mode="before")
    @classmethod
    def _problemas(cls, v):
        if isinstance(v, str):
            v = [v]
        return [str(x)[:160] for x in (v or [])][:3]

    @field_validator("correccion", mode="before")
    @classmethod
    def _correccion(cls, v):
        v = (str(v).strip() if v is not None else "")
        return v[:300] if v and v.lower() not in ("null", "none") else None


def _sin_datos(turno: Turno) -> bool:
    """Sin tools y sin cifras: no afirma datos de la fuente (repregunta, saludo, 123)."""
    return not turno.tools and not _DIGITO.search(turno.respuesta)


def _contexto(turno: Turno) -> str:
    tools = [{"tool": t.nombre, "args": t.args, "resultado": t.resultado[:MAX_RESULTADO]} for t in turno.tools]
    return (f"PREGUNTA DEL USUARIO:\n{turno.pregunta or '(sin texto)'}\n\n"
            f"RESULTADOS DE TOOLS:\n{json.dumps(tools, ensure_ascii=False, indent=1) if tools else '(ninguna tool)'}\n\n"
            f"RESPUESTA DEL ASISTENTE:\n{turno.respuesta}")


async def verificar(turno: Turno) -> Veredicto | None:
    """Veredicto del turno, o None si no se pudo verificar (Groq caído/lento): mejor no marcar nada que
    marcar mal. Nunca lanza (salvo cancelación por interrupción)."""
    if not turno.respuesta.strip():
        return None
    if _sin_datos(turno):
        return Veredicto(estado="fuera_de_datos_ok")
    try:
        v = await llm.astructured(_contexto(turno), Veredicto, model=MODELO, system=SISTEMA, retries=1,
                                  total_timeout=TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        log.warning("Verificador no disponible (%s: %s)", type(e).__name__, str(e)[:120])
        return None
    if v.estado != "no_respaldado":
        v = v.model_copy(update={"correccion": None})
    return v
