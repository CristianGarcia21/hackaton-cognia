"""Contrato del WebSocket /ws/voz: FUENTE DE VERDAD entre frontend y backend (spec §6).

Navegador → backend
    - Frames BINARIOS: audio del micrófono, ver AUDIO_ENTRADA (PCM16 LE mono 16 kHz, trozos de 20-40 ms).
    - JSON: Start, Stop, TextInput.
Backend → navegador
    - Frames BINARIOS: audio del agente (TTS), ver AUDIO_SALIDA (PCM16 LE mono 24 kHz).
    - JSON: un evento por mensaje, con el campo "type" (ver EVENTOS_SERVIDOR).

Cada modelo trae ejemplos válidos en `ejemplos(Modelo)`; los usan el mock (#3) y los tests.
Para ver todos los ejemplos en JSON:  uv run python -m server.events

Reglas: campos extra prohibidos; los opcionales en None no se envían (to_json).
Si necesitas un evento o campo nuevo, cámbialo AQUÍ y avisa a la otra persona.
"""

import json
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

AUDIO_ENTRADA = {"encoding": "linear16", "sample_rate": 16000, "channels": 1}
AUDIO_SALIDA = {"encoding": "linear16", "sample_rate": 24000, "channels": 1}


class EstadoConversacion(StrEnum):
    """Estados de la máquina de estados de la sesión (spec §5.4)."""

    INACTIVO = "inactivo"
    ESCUCHANDO = "escuchando"
    PENSANDO = "pensando"
    EJECUTANDO_TOOL = "ejecutando_tool"
    HABLANDO = "hablando"
    INTERRUMPIDO = "interrumpido"


EmocionNombre = Literal["alegria", "calma", "neutral", "confusion", "ansiedad", "frustracion", "enojo",
                        "tristeza", "urgencia"]
ReglaAdaptacion = Literal["urgencia", "frustracion", "ansiedad", "confusion", "normal"]
Unidad = Annotated[float, Field(ge=0, le=1)]


def _ejemplos(*items: dict) -> ConfigDict:
    return ConfigDict(extra="forbid", json_schema_extra={"examples": list(items)})


# ============================ Navegador → backend ============================

class Start(BaseModel):
    """Empieza a capturar el micrófono; desde aquí llegan frames binarios de audio."""

    model_config = _ejemplos({"type": "start"})
    type: Literal["start"] = "start"


class Stop(BaseModel):
    """Deja de capturar el micrófono."""

    model_config = _ejemplos({"type": "stop"})
    type: Literal["stop"] = "stop"


class TextInput(BaseModel):
    """Respaldo por texto o pregunta sugerida pulsada: se procesa como si el usuario la hubiera dicho."""

    model_config = _ejemplos({"type": "text_input", "text": "¿Qué IPS tienen sala de cirugía en Medellín?"})
    type: Literal["text_input"] = "text_input"
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def _no_vacio(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("el texto no puede estar vacío")
        return v.strip()


# ============================ Backend → navegador ============================

class Ready(BaseModel):
    """La sesión está lista: el navegador puede habilitar el micrófono."""

    model_config = _ejemplos({"type": "ready", "session_id": "a1b2c3", "voice": "aura-2-celeste-es",
                              "sources": ["ips", "citas", "excel", "calendario"]})
    type: Literal["ready"] = "ready"
    session_id: str
    voice: str
    sources: list[str] = Field(description="servidores MCP / fuentes conectadas")


class State(BaseModel):
    """Cambio de estado de la conversación (indicador grande de la UI)."""

    model_config = _ejemplos({"type": "state", "state": "pensando", "turn_id": 4},
                             {"type": "state", "state": "inactivo"})
    type: Literal["state"] = "state"
    state: EstadoConversacion
    turn_id: int | None = None


class AudioFlush(BaseModel):
    """Interrupción: vaciar YA el búfer de reproducción (los frames de ese turno se descartan)."""

    model_config = _ejemplos({"type": "audio_flush", "turn_id": 4})
    type: Literal["audio_flush"] = "audio_flush"
    turn_id: int


class Transcript(BaseModel):
    """Transcripción diarizada. Los parciales (is_final=false) se reemplazan por el final del mismo segmento."""

    model_config = _ejemplos(
        {"type": "transcript", "turn_id": 4, "speaker": "Hablante 1",
         "text": "Necesito una cita para una cirugía en Medellín", "start": 42.1, "end": 45.3, "is_final": True},
        {"type": "transcript", "turn_id": 4, "speaker": "Agente",
         "text": "Encontré cinco IPS en Medellín con sala de cirugía.", "start": 46.4, "end": 49.0,
         "is_final": True},
    )
    type: Literal["transcript"] = "transcript"
    turn_id: int | None = None
    speaker: str = Field(description='"Hablante N" o "Agente"')
    text: str
    start: float = Field(ge=0, description="segundos desde el inicio de la sesión")
    end: float = Field(ge=0)
    is_final: bool


class AgentText(BaseModel):
    """Texto de lo que dice (o empezó a decir) el agente en un turno."""

    model_config = _ejemplos({"type": "agent_text", "turn_id": 4,
                              "text": "Encontré cinco IPS en Medellín con sala de cirugía."})
    type: Literal["agent_text"] = "agent_text"
    turn_id: int
    text: str


class Emotion(BaseModel):
    """Sentimiento y emoción de un turno de un hablante (spec §9.2)."""

    model_config = _ejemplos({"type": "emotion", "turn_id": 4, "speaker": "Hablante 1", "sentiment": -0.3,
                              "emotion": "ansiedad", "intensity": 0.6, "signals": ["cirugía", "urgente"]})
    type: Literal["emotion"] = "emotion"
    turn_id: int
    speaker: str
    sentiment: float = Field(ge=-1, le=1)
    emotion: EmocionNombre
    intensity: Unidad
    signals: list[str] = []


class Adaptation(BaseModel):
    """Regla de adaptación vigente (spec §9.3). Se emite solo cuando cambia."""

    model_config = _ejemplos({"type": "adaptation", "active": True, "rule": "ansiedad",
                              "style": "Cálido y tranquilizador", "speed": 0.9,
                              "reason": "ansiedad detectada (sentimiento -0.3)"})
    type: Literal["adaptation"] = "adaptation"
    active: bool
    rule: ReglaAdaptacion
    style: str
    speed: float = Field(ge=0.7, le=1.5)
    reason: str


class Verification(BaseModel):
    """Veredicto del verificador QA sobre la respuesta de un turno (spec §9.4)."""

    model_config = _ejemplos(
        {"type": "verification", "turn_id": 4, "status": "respaldado", "issues": []},
        {"type": "verification", "turn_id": 5, "status": "no_respaldado",
         "issues": ["La cifra 120 no aparece en el resultado de la tool"],
         "correction": "Según los datos son 112 camas."},
    )
    type: Literal["verification"] = "verification"
    turn_id: int
    status: Literal["respaldado", "parcial", "no_respaldado", "fuera_de_datos_ok"]
    issues: list[str] = []
    correction: str | None = None


class ToolCall(BaseModel):
    """Llamada a una tool (para el Inspector). Se emite al iniciar (running) y al terminar."""

    model_config = _ejemplos(
        {"type": "tool", "turn_id": 4, "name": "buscar_ips",
         "args": {"municipio": "Medellín", "capacidad": "Sala de Cirugía"}, "status": "running"},
        {"type": "tool", "turn_id": 4, "name": "buscar_ips",
         "args": {"municipio": "Medellín", "capacidad": "Sala de Cirugía"}, "status": "ok", "ms": 12,
         "summary": "5 sedes encontradas"},
    )
    type: Literal["tool"] = "tool"
    turn_id: int
    name: str
    args: dict[str, Any] = {}
    status: Literal["running", "ok", "error", "timeout", "cancelled"]
    ms: int | None = Field(default=None, ge=0)
    summary: str | None = None


class Action(BaseModel):
    """Acción realizada en un sistema conectado (panel de acciones)."""

    model_config = _ejemplos({"type": "action", "kind": "cita",
                              "data": {"id": 7, "paciente": "María", "sede": "Hospital X",
                                       "estado": "pendiente de confirmación por la IPS"}},
                             {"type": "action", "kind": "excel", "data": {"filas": 3},
                              "link": "/api/citas/excel"})
    type: Literal["action"] = "action"
    kind: Literal["cita", "evento", "excel"]
    data: dict[str, Any]
    link: str | None = None


class Span(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str = Field(description="fin_turno, llm_decide, tool:<nombre>, llm_redacta, primer_audio, ...")
    ms: int = Field(ge=0)
    detail: str | None = None


class Trace(BaseModel):
    """Traza de un turno: cascada de tiempos y contexto usado (spec §11.1)."""

    model_config = _ejemplos({
        "type": "trace", "turn_id": 4,
        "spans": [{"stage": "fin_turno", "ms": 380}, {"stage": "llm_decide", "ms": 190},
                  {"stage": "tool:buscar_ips", "ms": 12, "detail": "municipio=Medellín · 5 filas"},
                  {"stage": "llm_redacta", "ms": 210}, {"stage": "primer_audio", "ms": 240}],
        "context": {"adaptacion": "ansiedad", "lecciones": 2, "tools": ["buscar_ips"]},
    })
    type: Literal["trace"] = "trace"
    turn_id: int
    spans: list[Span]
    context: dict[str, Any] = {}


class Brief(BaseModel):
    """Brief de la fuente de datos con 3 a 5 preguntas sugeridas (spec §9.5)."""

    model_config = _ejemplos({
        "type": "brief",
        "summary": "Registro de 10 921 sedes de IPS de Colombia con su capacidad instalada (corte nov. 2022).",
        "key_points": ["41 427 registros de capacidad", "Nivel de atención sin dato en el 61 %"],
        "questions": ["¿Cuántas camas de UCI hay en Antioquia?", "¿Qué IPS públicas tienen quirófano en Cali?",
                      "¿Cómo se distribuyen las ambulancias por departamento?"],
        "stats": {"filas": 41427, "sedes": 10921, "prestadores": 9320},
    })
    type: Literal["brief"] = "brief"
    summary: str
    key_points: list[str]
    questions: list[str] = Field(min_length=3, max_length=5)
    stats: dict[str, Any] = {}


class SourceStatus(BaseModel):
    """Progreso de carga de una fuente (narrativa visible del paso P2)."""

    model_config = _ejemplos({"type": "source_status", "source": "datos.gov.co", "status": "descargando",
                              "rows": 21000, "pages": 21, "progress": 0.5})
    type: Literal["source_status"] = "source_status"
    source: str
    status: Literal["conectando", "descargando", "listo", "respaldo", "error"]
    rows: int = Field(default=0, ge=0)
    pages: int = Field(default=0, ge=0)
    progress: Unidad = 0


class Lesson(BaseModel):
    """Lección aprendida (spec §10.3)."""

    model_config = _ejemplos({"type": "lesson", "kind": "keyterm", "content": "Zipaquirá",
                              "origin": "corrección del usuario"})
    type: Literal["lesson"] = "lesson"
    kind: Literal["keyterm", "sinonimo", "regla"]
    content: str
    origin: str


class Error(BaseModel):
    """Error visible y no bloqueante. recoverable=false → la UI ofrece reconectar."""

    model_config = _ejemplos({"type": "error", "where": "groq", "message": "Límite de uso; cambiando de modelo",
                              "recoverable": True})
    type: Literal["error"] = "error"
    where: str
    message: str
    recoverable: bool


# ================================ Registro y helpers ================================

EVENTOS_SERVIDOR: dict[str, type[BaseModel]] = {
    m.model_fields["type"].default: m
    for m in (Ready, State, AudioFlush, Transcript, AgentText, Emotion, Adaptation, Verification, ToolCall,
              Action, Trace, Brief, SourceStatus, Lesson, Error)
}
MENSAJES_CLIENTE: dict[str, type[BaseModel]] = {
    m.model_fields["type"].default: m for m in (Start, Stop, TextInput)
}

EventoServidor = Annotated[
    Ready | State | AudioFlush | Transcript | AgentText | Emotion | Adaptation | Verification | ToolCall
    | Action | Trace | Brief | SourceStatus | Lesson | Error,
    Field(discriminator="type"),
]
MensajeCliente = Annotated[Start | Stop | TextInput, Field(discriminator="type")]

_servidor = TypeAdapter(EventoServidor)
_cliente = TypeAdapter(MensajeCliente)


def parse_servidor(raw: str | bytes) -> BaseModel:
    """JSON → evento del servidor (lo usan el mock, los tests y el agente de pruebas QA)."""
    return _servidor.validate_json(raw)


def parse_cliente(raw: str | bytes) -> BaseModel:
    """JSON del navegador → Start | Stop | TextInput. Lanza ValidationError si no cumple el contrato."""
    return _cliente.validate_json(raw)


def to_json(evento: BaseModel) -> str:
    """Evento → JSON listo para enviar, sin los opcionales en None."""
    return evento.model_dump_json(exclude_none=True)


def ejemplos(modelo: type[BaseModel]) -> list[dict]:
    return modelo.model_config.get("json_schema_extra", {}).get("examples", [])


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    print("Audio entrada:", AUDIO_ENTRADA, "· salida:", AUDIO_SALIDA)
    for titulo, grupo in (("NAVEGADOR → BACKEND", MENSAJES_CLIENTE), ("BACKEND → NAVEGADOR", EVENTOS_SERVIDOR)):
        print(f"\n===== {titulo} =====")
        for modelo in grupo.values():
            for e in ejemplos(modelo):
                print(json.dumps(e, ensure_ascii=False))
