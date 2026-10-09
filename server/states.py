"""Máquina de estados de una sesión de voz (spec §5.4). Pura: no hace I/O, solo decide transiciones.

    m = Maquina()
    cambio = m.aplicar(Evento.USUARIO_HABLA)   # -> Cambio(estado, turn_id, interrumpido) o None

Eventos observados en Deepgram (2026-10-09): el Voice Agent NO envía AgentStartedSpeaking ni
AgentThinking con Groq; por eso "el agente habla" se deriva del primer audio TTS o de su
ConversationText, y "pensando" del ConversationText del usuario (fin de su turno).
"""

import logging
from dataclasses import dataclass
from enum import StrEnum

from server.events import EstadoConversacion as E

log = logging.getLogger("cognia.states")


class Evento(StrEnum):
    USUARIO_HABLA = "usuario_habla"          # UserStartedSpeaking
    USUARIO_TERMINA = "usuario_termina"      # ConversationText role=user (o AgentThinking)
    TOOL_PEDIDA = "tool_pedida"              # FunctionCallRequest
    TOOL_RESPONDIDA = "tool_respondida"      # se envió el último FunctionCallResponse pendiente
    TOOL_CANCELADA = "tool_cancelada"        # FunctionCallCancelled
    AGENTE_HABLA = "agente_habla"            # primer audio TTS o ConversationText role=assistant
    AUDIO_TERMINADO = "audio_terminado"      # AgentAudioDone


# Estado de destino por evento y estados de origen válidos. Lo que no está aquí se ignora (se registra).
TRANSICIONES: dict[Evento, tuple[set[E], E]] = {
    Evento.USUARIO_HABLA: (set(E), E.ESCUCHANDO),
    Evento.USUARIO_TERMINA: ({E.INACTIVO, E.ESCUCHANDO}, E.PENSANDO),
    Evento.TOOL_PEDIDA: ({E.INACTIVO, E.ESCUCHANDO, E.PENSANDO, E.EJECUTANDO_TOOL}, E.EJECUTANDO_TOOL),
    Evento.TOOL_RESPONDIDA: ({E.EJECUTANDO_TOOL}, E.PENSANDO),
    Evento.TOOL_CANCELADA: ({E.EJECUTANDO_TOOL, E.PENSANDO}, E.ESCUCHANDO),
    Evento.AGENTE_HABLA: ({E.INACTIVO, E.ESCUCHANDO, E.PENSANDO, E.EJECUTANDO_TOOL}, E.HABLANDO),
    Evento.AUDIO_TERMINADO: ({E.HABLANDO}, E.INACTIVO),
}

# Si el usuario habla mientras pasa algo de esto, el agente pierde el turno: interrupción (barge-in).
INTERRUMPIBLES = {E.HABLANDO, E.PENSANDO, E.EJECUTANDO_TOOL}


@dataclass(frozen=True)
class Cambio:
    estado: E
    turn_id: int
    interrumpido: bool = False  # venía hablando/pensando: vaciar audio, descartar lo del turno viejo


class Maquina:
    def __init__(self):
        self.estado = E.INACTIVO
        self.turn_id = 0

    def aplicar(self, evento: Evento) -> Cambio | None:
        """Aplica un evento. Devuelve el cambio (para emitir `state`) o None si no cambia nada."""
        origenes, destino = TRANSICIONES[evento]
        if self.estado not in origenes:
            log.debug("Evento %s ignorado en estado %s", evento, self.estado)
            return None
        interrumpido = False
        if evento is Evento.USUARIO_HABLA:
            interrumpido = self.estado in INTERRUMPIBLES
            self.turn_id += 1  # cada intervención del usuario abre un turno nuevo
        elif self.estado is destino:
            return None
        self.estado = destino
        return Cambio(destino, self.turn_id, interrumpido)
