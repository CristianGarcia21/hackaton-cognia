"""Traza por turno (spec §11.1): cascada de tiempos y contexto que usó el modelo, para el Inspector.

Responde "¿cómo usó el modelo el contexto y dónde falló?": qué preguntó el usuario, qué tools llamó el
LLM y con qué argumentos, cuánto devolvió cada una, cuánto tardó cada etapa, qué emoción y adaptación
había y qué dijo el verificador.

Tiempos de dos fuentes:
- Deepgram LatencyReport (llega en piezas): stt_latency → `fin_turno`, ttt_tool/ttt_token → `llm_decide`.
- Relojes del backend (time.perf_counter): tools, `llm_redacta`, `primer_audio`, `emocion`, `verificador`.

La UI recibe `trace` varias veces por turno (al terminar el audio y cuando terminan emoción y verificador):
la última con el mismo turn_id reemplaza a la anterior. El JSONL se escribe una vez, con la versión final.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from server import events as ev

log = logging.getLogger("cognia.trace")

DIR_TRAZAS = Path(__file__).resolve().parents[2] / "outputs" / "trazas"


def ahora() -> float:
    return time.perf_counter()


def _ms(segundos) -> float | None:
    try:
        return round(float(segundos) * 1000, 1)
    except (TypeError, ValueError):
        return None


@dataclass
class Traza:
    turn_id: int
    inicio: float = field(default_factory=ahora)        # UserStartedSpeaking
    fin_usuario: float | None = None                    # ConversationText del usuario (fin de turno)
    ultima_tool: float | None = None                    # se envió el último FunctionCallResponse
    primer_texto: float | None = None                   # primer ConversationText del agente
    primer_audio: float | None = None                   # primer audio TTS del turno
    latencias: dict = field(default_factory=dict)       # LatencyReport de Deepgram (segundos)
    tools: list[ev.Span] = field(default_factory=list)
    extra: list[ev.Span] = field(default_factory=list)  # emocion, adaptacion, verificador
    contexto: dict = field(default_factory=dict)
    pendientes: set = field(default_factory=lambda: {"audio"})  # lo que falta para escribir el JSONL
    guardada: bool = False

    def tool(self, nombre: str, ms: float, args: dict, resultado: str, status: str) -> None:
        detalle = " · ".join([", ".join(f"{k}={v}" for k, v in args.items()) or "sin argumentos",
                              f"{len(resultado)} caracteres", status])
        self.tools.append(ev.Span(stage=f"tool:{nombre}", ms=ms, detail=detalle[:200]))
        self.ultima_tool = ahora()
        self.contexto.setdefault("tools", []).append(nombre)
        self.contexto["caracteres_de_tools"] = self.contexto.get("caracteres_de_tools", 0) + len(resultado)

    def etapa(self, nombre: str, ms: float, detalle: str | None = None) -> None:
        self.extra.append(ev.Span(stage=nombre, ms=ms, detail=detalle))

    def spans(self) -> list[ev.Span]:
        s: list[ev.Span] = []
        if (stt := _ms(self.latencias.get("stt_latency"))) is not None:
            s.append(ev.Span(stage="fin_turno", ms=stt, detail="Deepgram: STT y detección del fin de turno"))
        clave = "ttt_tool_latency" if self.tools else "ttt_token_latency"
        if (decide := _ms(self.latencias.get(clave))) is not None:
            s.append(ev.Span(stage="llm_decide", ms=decide,
                             detail="Groq: hasta pedir la tool" if self.tools else "Groq: primer token"))
        s += self.tools
        if self.tools and self.ultima_tool and self.primer_texto and self.primer_texto > self.ultima_tool:
            s.append(ev.Span(stage="llm_redacta", ms=(self.primer_texto - self.ultima_tool) * 1000,
                             detail="Groq: redacta con el resultado de la tool"))
        elif not self.tools and (redacta := _ms(self.latencias.get("ttt_text_latency"))) is not None:
            s.append(ev.Span(stage="llm_redacta", ms=redacta, detail="Groq: respuesta completa"))
        if self.fin_usuario and self.primer_audio and self.primer_audio > self.fin_usuario:
            s.append(ev.Span(stage="primer_audio", ms=(self.primer_audio - self.fin_usuario) * 1000,
                             detail="desde el fin del turno del usuario hasta el primer audio"))
        return s + self.extra

    def evento(self) -> ev.Trace:
        return ev.Trace(turn_id=self.turn_id, spans=self.spans(), context=self.contexto)

    def listo_para_guardar(self) -> bool:
        return not self.pendientes and not self.guardada

    def guardar(self, session_id: str, directorio: Path | None = None) -> None:
        """Agrega la traza final al JSONL de la sesión. Nunca lanza: la traza no debe tumbar la voz."""
        self.guardada = True
        directorio = directorio or DIR_TRAZAS
        try:
            directorio.mkdir(parents=True, exist_ok=True)
            linea = {"session_id": session_id, "ts": time.time(), **self.evento().model_dump(mode="json")}
            with open(directorio / f"{session_id}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(linea, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("No se pudo guardar la traza: %s", e)
