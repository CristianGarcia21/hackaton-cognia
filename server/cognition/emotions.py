"""Emociones por turno del usuario (spec §9.2). Nunca bloquea la voz: corre en segundo plano.

    emo = await analizar("¡Llevo una hora esperando y nadie me ayuda!")   # -> Emocion
    estado.agregar(emo); estado.sentimiento_medio  # promedio móvil de los últimos 3 turnos

Dos capas: (1) reglas instantáneas para riesgo vital (no dependen del LLM: una urgencia nunca espera
a Groq ni se pierde si Groq falla) y (2) el LLM estructurado para el matiz (sentimiento, emoción,
intensidad). Si el LLM falla o tarda, queda lo de las reglas o "neutral".
"""

import logging
import re
from collections import deque
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, field_validator

from core import llm
from server.events import EmocionNombre, normalizar_emocion

log = logging.getLogger("cognia.emotions")

MODELO = "groq/openai/gpt-oss-20b"
TIMEOUT_S = 4.0
VENTANA = 3

# Señales de riesgo vital: si aparecen, la regla de urgencia aplica aunque el LLM no responda.
RIESGO_VITAL = [r"dolor (fuerte )?(en el|de) pecho", r"no (puede|puedo) respirar", r"le falta el aire",
                r"me falta el aire", r"sangr(a|ado|ando) mucho", r"inconscient", r"desmay", r"convulsi",
                r"infarto", r"derrame", r"suicid", r"quitarme la vida", r"se est[aá] muriendo", r"envenen",
                r"no reacciona", r"accidente grave", r"urgencia", r"urgente", r"emergencia"]
_RIESGO = re.compile("|".join(RIESGO_VITAL), re.IGNORECASE)

SISTEMA = """Analiza la emoción de UNA intervención de un usuario que habla con un orientador de salud por voz.
Devuelve solo JSON con:
- sentimiento: número de -1 (muy negativo) a 1 (muy positivo)
- emocion: una de alegria, calma, neutral, confusion, ansiedad, frustracion, enojo, tristeza, urgencia
- intensidad: número de 0 a 1
- senales: hasta 3 palabras o frases cortas del texto que justifican la emoción
Usa "urgencia" solo si describe una emergencia médica en curso. "confusion" si no entiende o repregunta.
Una pregunta neutra de información es "neutral" con sentimiento cercano a 0."""


class Emocion(BaseModel):
    """Salida del LLM. Laxa a propósito: recorta y normaliza en vez de rechazar (el LLM a veces devuelve
    sentimiento=8.0 o "Miedo")."""

    sentimiento: float = 0.0
    emocion: EmocionNombre = "neutral"
    intensidad: float = 0.0
    senales: list[str] = Field(default_factory=list)

    @field_validator("sentimiento", mode="before")
    @classmethod
    def _sent(cls, v):
        v = _num(v)
        if abs(v) > 1:  # escala 0-10 o -10..10: se lleva a [-1, 1]
            v = v / 10
        return max(-1.0, min(1.0, v))

    @field_validator("intensidad", mode="before")
    @classmethod
    def _int(cls, v):
        v = _num(v)
        return max(0.0, min(1.0, v / 10 if v > 1 else v))

    @field_validator("emocion", mode="before")
    @classmethod
    def _emo(cls, v):
        return normalizar_emocion(v)

    @field_validator("senales", mode="before")
    @classmethod
    def _sen(cls, v):
        if isinstance(v, str):
            v = [v]
        return list(dict.fromkeys(str(x)[:40] for x in (v or [])))[:3]  # sin repetidas


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def riesgo_vital(texto: str) -> list[str]:
    return [m.group(0).lower() for m in _RIESGO.finditer(texto or "")][:3]


async def analizar(texto: str) -> Emocion:
    """Emoción de un turno. Nunca lanza (salvo cancelación)."""
    riesgo = riesgo_vital(texto)
    try:
        emo = await llm.astructured(texto, Emocion, model=MODELO, system=SISTEMA, retries=1,
                                    total_timeout=TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — Groq caído o lento: no se pierde la urgencia
        log.warning("Emoción por LLM no disponible (%s: %s)", type(e).__name__, str(e)[:120])
        emo = Emocion()
    if riesgo:
        emo = emo.model_copy(update={"emocion": "urgencia", "intensidad": max(emo.intensidad, 0.8),
                                     "sentimiento": min(emo.sentimiento, -0.3),
                                     "senales": list(dict.fromkeys(riesgo + emo.senales))[:3]})
    return emo


@dataclass
class EstadoAfectivo:
    """Promedio móvil de los últimos turnos del usuario (spec §9.2)."""

    turnos: deque = field(default_factory=lambda: deque(maxlen=VENTANA))

    def agregar(self, emo: Emocion) -> None:
        self.turnos.append(emo)

    @property
    def sentimiento_medio(self) -> float:
        return sum(e.sentimiento for e in self.turnos) / len(self.turnos) if self.turnos else 0.0

    @property
    def tendencia(self) -> float:
        """Positiva si mejora, negativa si empeora (último menos primero de la ventana)."""
        return self.turnos[-1].sentimiento - self.turnos[0].sentimiento if len(self.turnos) > 1 else 0.0

    @property
    def ultima(self) -> Emocion | None:
        return self.turnos[-1] if self.turnos else None
