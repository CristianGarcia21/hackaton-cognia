"""Contrato del WebSocket /ws/voz: FUENTE DE VERDAD entre frontend y backend (spec §6).

Navegador → backend
    - Frames BINARIOS: audio del micrófono, ver AUDIO_ENTRADA (PCM16 LE mono 16 kHz, trozos de 20-40 ms).
    - JSON: Start, Stop, TextInput. `text_input` funciona aunque el micrófono no esté activo;
      `start`/`stop` repetidos no tienen efecto.
    - Un mensaje inválido NO cierra la conexión: el backend responde un `error` con where="cliente"
      y recoverable=true (ver parse_cliente_seguro).
Backend → navegador
    - Frames BINARIOS: audio del agente (TTS), ver AUDIO_SALIDA (PCM16 LE mono 24 kHz).
    - JSON: un evento por mensaje, con el campo "type" (ver EVENTOS_SERVIDOR).

Reglas para el frontend
    - Los campos opcionales SIN valor no se envían: en JS llegan como `undefined`, nunca como `null`.
      (Un null DENTRO de args/data/context sí se conserva.)
    - turn_id: lo asigna la sesión del backend; empieza en 1 y aumenta cuando el agente empieza a
      procesar un turno nuevo del usuario (fin de turno / AgentThinking, spec §5.4). Puede faltar en
      eventos que no pertenecen a un turno del agente (p. ej. transcripción diarizada).
    - transcript: los parciales y el final de un mismo segmento comparten `segment_id`; la UI
      reemplaza la burbuja con ese id y la cierra cuando llega is_final=true.
    - audio_flush: el BACKEND deja de enviar los frames del turno interrumpido antes de emitirlo;
      la UI solo vacía su búfer de reproducción.
    - Contrato completo para JS (esquemas, enums y ejemplos): web/contrato.json, que se regenera con
      `uv run python -m server.events --exportar` (un test falla si queda desactualizado).

Robustez: los valores que generan LLMs o relojes se NORMALIZAN o RECORTAN en vez de rechazarse
(un ValidationError en plena demo es peor que un 1.0000001 recortado a 1). Lo que manda el cliente sí
se valida estrictamente. Si necesitas un evento o campo nuevo, cámbialo AQUÍ, exporta y avisa.
"""

import json
import math
import sys
import unicodedata
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, get_args

from pydantic import (BaseModel, BeforeValidator, ConfigDict, Field, TypeAdapter, ValidationError,
                      field_validator, model_validator)

AUDIO_ENTRADA = {"encoding": "linear16", "sample_rate": 16000, "channels": 1}
AUDIO_SALIDA = {"encoding": "linear16", "sample_rate": 24000, "channels": 1}
CONTRATO_JSON = Path(__file__).resolve().parent.parent / "web" / "contrato.json"


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
EstadoVerificacion = Literal["respaldado", "parcial", "no_respaldado", "fuera_de_datos_ok"]

# Sinónimos frecuentes que devuelve un LLM -> emoción de la spec (§9.2, §9.3 "ansiedad o miedo").
_SINONIMOS_EMOCION = {
    "miedo": "ansiedad", "temor": "ansiedad", "preocupacion": "ansiedad", "nervios": "ansiedad",
    "estres": "ansiedad", "ira": "enojo", "rabia": "enojo", "molestia": "frustracion",
    "felicidad": "alegria", "satisfaccion": "alegria", "tranquilidad": "calma", "duda": "confusion",
}


def _clave(v: Any) -> str:
    """'Frustración ' -> 'frustracion'; 'fuera de datos ok' -> 'fuera_de_datos_ok'."""
    sin_tildes = unicodedata.normalize("NFKD", str(v)).encode("ascii", "ignore").decode()
    return "_".join(sin_tildes.strip().lower().replace("-", " ").split())


def _normalizar(validos: tuple[str, ...], por_defecto: str, sinonimos: dict[str, str] | None = None):
    def validar(v: Any) -> str:
        k = _clave(v)
        k = (sinonimos or {}).get(k, k)
        return k if k in validos else por_defecto
    return BeforeValidator(validar)


def normalizar_emocion(v: Any) -> str:
    """La misma normalización que usa Emotion (tildes, sinónimos; desconocida → "neutral")."""
    k = _clave(v)
    k = _SINONIMOS_EMOCION.get(k, k)
    return k if k in get_args(EmocionNombre) else "neutral"


def _numero(v: Any, si_nan: float) -> float:
    """Número o ValueError (→ ValidationError). bool/None/texto no numérico se rechazan; NaN → si_nan."""
    if v is None or isinstance(v, bool):
        raise ValueError("se esperaba un número")
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise ValueError("se esperaba un número") from None
    return si_nan if math.isnan(n) else n


def _recortar(minimo: float, maximo: float):
    neutro = 0.0 if minimo <= 0 <= maximo else minimo
    return BeforeValidator(lambda v: min(maximo, max(minimo, _numero(v, neutro))))


Unidad = Annotated[float, _recortar(0, 1)]
Ms = Annotated[int, BeforeValidator(lambda v: max(0, round(_numero(v, 0))))]


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
    """Respaldo por texto o pregunta sugerida pulsada: se procesa como si el usuario la hubiera dicho.
    Se quitan los espacios de los extremos y se recorta a 2000 caracteres; vacío → inválido."""

    model_config = _ejemplos({"type": "text_input", "text": "¿Qué IPS tienen sala de cirugía en Medellín?"})
    type: Literal["text_input"] = "text_input"
    text: str = Field(min_length=1)

    @field_validator("text", mode="before")
    @classmethod
    def _limpiar(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("text debe ser un string")
        return v.strip()[:2000]


# ============================ Backend → navegador ============================

class Ready(BaseModel):
    """La sesión está lista: el navegador puede habilitar el micrófono."""

    model_config = _ejemplos({"type": "ready", "session_id": "a1b2c3", "voice": "aura-2-celeste-es",
                              "sources": ["ips", "citas", "excel", "calendario"]})
    type: Literal["ready"] = "ready"
    session_id: str
    voice: str
    sources: list[str] = Field(description="servidores MCP / fuentes conectadas")


class EstadoEvento(BaseModel):
    """Cambio de estado de la conversación (indicador grande de la UI). type = "state"."""

    model_config = _ejemplos({"type": "state", "state": "pensando", "turn_id": 4},
                             {"type": "state", "state": "inactivo"})
    type: Literal["state"] = "state"
    state: EstadoConversacion
    turn_id: int | None = None


class AudioFlush(BaseModel):
    """Interrupción: la UI vacía YA su búfer de reproducción. El backend ya dejó de enviar los
    frames de ese turno (los frames binarios no llevan turn_id)."""

    model_config = _ejemplos({"type": "audio_flush", "turn_id": 4})
    type: Literal["audio_flush"] = "audio_flush"
    turn_id: int


class Transcript(BaseModel):
    """Transcripción diarizada. Parciales y final de un segmento comparten segment_id: la UI reemplaza
    la burbuja con ese id. start/end en segundos desde el inicio de la sesión; pueden faltar (p. ej. en
    el texto del Agente) y entonces la UI usa la hora de llegada."""

    model_config = _ejemplos(
        {"type": "transcript", "segment_id": "h-12", "turn_id": 4, "speaker": "Hablante 1",
         "text": "Necesito una cita para una cirugía en Medellín", "start": 42.1, "end": 45.3, "is_final": True},
        {"type": "transcript", "segment_id": "a-4", "turn_id": 4, "speaker": "Agente",
         "text": "Encontré cinco IPS en Medellín con sala de cirugía.", "is_final": True},
    )
    type: Literal["transcript"] = "transcript"
    segment_id: str
    turn_id: int | None = None
    speaker: str = Field(description='"Hablante N" o "Agente"')
    text: str
    start: Annotated[float, _recortar(0, float("inf"))] | None = None
    end: Annotated[float, _recortar(0, float("inf"))] | None = None
    is_final: bool

    @model_validator(mode="after")
    def _end_no_menor_que_start(self) -> "Transcript":
        if self.start is not None and self.end is not None and self.end < self.start:
            self.end = self.start
        return self


class AgentText(BaseModel):
    """Texto de lo que dice (o empezó a decir) el agente en un turno."""

    model_config = _ejemplos({"type": "agent_text", "turn_id": 4,
                              "text": "Encontré cinco IPS en Medellín con sala de cirugía."})
    type: Literal["agent_text"] = "agent_text"
    turn_id: int
    text: str


class Emotion(BaseModel):
    """Sentimiento y emoción de un turno de un hablante (spec §9.2). Rangos recortados y emoción
    normalizada (tildes, mayúsculas, sinónimos como "miedo" → "ansiedad"; desconocida → "neutral")."""

    model_config = _ejemplos({"type": "emotion", "turn_id": 4, "speaker": "Hablante 1", "sentiment": -0.3,
                              "emotion": "ansiedad", "intensity": 0.6, "signals": ["cirugía", "urgente"]})
    type: Literal["emotion"] = "emotion"
    turn_id: int | None = None
    speaker: str
    sentiment: Annotated[float, _recortar(-1, 1)]
    emotion: Annotated[EmocionNombre, _normalizar(get_args(EmocionNombre), "neutral", _SINONIMOS_EMOCION)]
    intensity: Unidad
    signals: list[str] = []


class Adaptation(BaseModel):
    """Regla de adaptación vigente (spec §9.3). Se emite solo cuando cambia.
    active se DERIVA: es true si rule != "normal". speed se recorta a 0.7..1.5."""

    model_config = _ejemplos({"type": "adaptation", "active": True, "rule": "ansiedad",
                              "style": "Cálido y tranquilizador", "speed": 0.9,
                              "reason": "ansiedad detectada (sentimiento -0.3)"})
    type: Literal["adaptation"] = "adaptation"
    active: bool = False
    rule: Annotated[ReglaAdaptacion, _normalizar(get_args(ReglaAdaptacion), "normal")]
    style: str
    speed: Annotated[float, _recortar(0.7, 1.5)]
    reason: str

    @model_validator(mode="after")
    def _derivar_active(self) -> "Adaptation":
        self.active = self.rule != "normal"
        return self


class Verification(BaseModel):
    """Veredicto del verificador QA sobre la respuesta de un turno (spec §9.4).
    status normalizado; un valor desconocido se trata como "parcial"."""

    model_config = _ejemplos(
        {"type": "verification", "turn_id": 4, "status": "respaldado", "issues": []},
        {"type": "verification", "turn_id": 5, "status": "no_respaldado",
         "issues": ["La cifra 120 no aparece en el resultado de la tool"],
         "correction": "Según los datos son 112 camas."},
    )
    type: Literal["verification"] = "verification"
    turn_id: int
    status: Annotated[EstadoVerificacion, _normalizar(get_args(EstadoVerificacion), "parcial")]
    issues: list[str] = []
    correction: str | None = None


class ToolCall(BaseModel):
    """Llamada a una tool (para el Inspector). Se emite al iniciar (running) y al terminar.
    ms acepta decimales y se redondea."""

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
    ms: Ms | None = None
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
    """Una etapa del turno: fin_turno, llm_decide, tool:<nombre>, llm_redacta, primer_audio, verificador,
    emocion, adaptacion. ms acepta decimales y se redondea."""

    model_config = ConfigDict(extra="forbid")
    stage: str
    ms: Ms
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


def _hasta_5(v: Any) -> Any:
    if isinstance(v, str):
        return [v]
    return list(v)[:5] if isinstance(v, (list, tuple)) else v


class Brief(BaseModel):
    """Brief de la fuente de datos (spec §9.5). El objetivo son 3 a 5 preguntas: cognition/brief.py
    garantiza al menos 3; aquí solo se recortan a 5 y se exige al menos 1."""

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
    questions: Annotated[list[str], BeforeValidator(_hasta_5), Field(min_length=1)]
    stats: dict[str, Any] = {}


class SourceStatus(BaseModel):
    """Progreso de carga de una fuente (paso P2). rows y pages son ACUMULADOS; total_rows es el total
    esperado si se conoce; progress (0..1, recortado) es la fracción lista."""

    model_config = _ejemplos({"type": "source_status", "source": "datos.gov.co", "status": "descargando",
                              "rows": 21000, "total_rows": 41427, "pages": 21, "progress": 0.5})
    type: Literal["source_status"] = "source_status"
    source: str
    status: Literal["conectando", "descargando", "listo", "respaldo", "error"]
    rows: int = Field(default=0, ge=0)
    total_rows: int | None = Field(default=None, ge=0)
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


class ErrorEvento(BaseModel):
    """Error visible y no bloqueante. type = "error". recoverable=false → la UI ofrece reconectar."""

    model_config = _ejemplos({"type": "error", "where": "groq", "message": "Límite de uso; cambiando de modelo",
                              "recoverable": True})
    type: Literal["error"] = "error"
    where: str
    message: str
    recoverable: bool


# ================================ Registro y helpers ================================

EVENTOS_SERVIDOR: dict[str, type[BaseModel]] = {
    m.model_fields["type"].default: m
    for m in (Ready, EstadoEvento, AudioFlush, Transcript, AgentText, Emotion, Adaptation, Verification,
              ToolCall, Action, Trace, Brief, SourceStatus, Lesson, ErrorEvento)
}
MENSAJES_CLIENTE: dict[str, type[BaseModel]] = {
    m.model_fields["type"].default: m for m in (Start, Stop, TextInput)
}

EventoServidor = Annotated[
    Ready | EstadoEvento | AudioFlush | Transcript | AgentText | Emotion | Adaptation | Verification | ToolCall
    | Action | Trace | Brief | SourceStatus | Lesson | ErrorEvento,
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


def parse_cliente_seguro(raw: str | bytes) -> BaseModel:
    """Como parse_cliente pero nunca lanza: si el mensaje es inválido devuelve un ErrorEvento
    recuperable para enviárselo al navegador sin cerrar el WebSocket."""
    try:
        return parse_cliente(raw)
    except ValidationError as e:
        detalle = e.errors()[0].get("msg", "mensaje inválido") if e.errors() else "mensaje inválido"
        return ErrorEvento(where="cliente", message=f"Mensaje no válido: {detalle}", recoverable=True)


def to_json(evento: BaseModel) -> str:
    """Evento → JSON listo para enviar, sin los opcionales en None."""
    return evento.model_dump_json(exclude_none=True)


def ejemplos(modelo: type[BaseModel]) -> list[dict]:
    return modelo.model_config.get("json_schema_extra", {}).get("examples", [])


def contrato_para_frontend() -> dict:
    """Todo lo que el frontend JS necesita, sin leer Python: audio, enums, esquemas y ejemplos."""
    return {
        "audio": {"entrada": AUDIO_ENTRADA, "salida": AUDIO_SALIDA},
        "estados": [e.value for e in EstadoConversacion],
        "emociones": list(get_args(EmocionNombre)),
        "reglas_adaptacion": list(get_args(ReglaAdaptacion)),
        "estados_verificacion": list(get_args(EstadoVerificacion)),
        "ejemplos": {
            "cliente": {t: ejemplos(m) for t, m in MENSAJES_CLIENTE.items()},
            "servidor": {t: ejemplos(m) for t, m in EVENTOS_SERVIDOR.items()},
        },
        "schema": {"cliente": _cliente.json_schema(), "servidor": _servidor.json_schema()},
    }


def exportar_contrato(destino: Path = CONTRATO_JSON) -> Path:
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(contrato_para_frontend(), ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
    return destino


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if "--exportar" in sys.argv:
        print("Contrato exportado en", exportar_contrato())
        sys.exit(0)
    print("Audio entrada:", AUDIO_ENTRADA, "· salida:", AUDIO_SALIDA)
    for titulo, grupo in (("NAVEGADOR → BACKEND", MENSAJES_CLIENTE), ("BACKEND → NAVEGADOR", EVENTOS_SERVIDOR)):
        print(f"\n===== {titulo} =====")
        for modelo in grupo.values():
            for e in ejemplos(modelo):
                print(json.dumps(e, ensure_ascii=False))
