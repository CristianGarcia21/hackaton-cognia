"""Política de adaptación afectiva (spec §9.3): tabla de reglas DETERMINISTA y explicable.

    politica = Politica(voz="aura-2-celeste-es")
    cambio = politica.evaluar(estado_afectivo)   # -> Cambio | None (None si la regla no cambió)
    if cambio: enviar(cambio.mensajes); emitir(cambio.evento)

Solo actúa cuando cambia la regla. Como UpdatePrompt AGREGA al prompt (no reemplaza), cada directiva
dice explícitamente que reemplaza el ajuste anterior. Aplica desde el siguiente turno del agente.
"""

from dataclasses import dataclass

from server import events as ev
from server.cognition.emotions import EstadoAfectivo

PREFIJO = "AJUSTE DE ESTILO VIGENTE (reemplaza cualquier ajuste de estilo anterior): "


@dataclass(frozen=True)
class Regla:
    nombre: str
    estilo: str      # para la UI
    velocidad: float
    directiva: str   # para el LLM (UpdatePrompt)


REGLAS = {
    "urgencia": Regla("urgencia", "Calmado y firme", 0.95,
                      "la persona describe una posible emergencia. Habla calmado y firme. Lo PRIMERO es indicar "
                      "que llame ya a la línea 123 o vaya a urgencias; solo después ofrece buscar la IPS más "
                      "cercana con urgencias o UCI. Nada de trámites ni citas antes de eso."),
    "frustracion": Regla("frustracion", "Frases cortas, reconoce la molestia", 1.0,
                         "la persona está frustrada. Reconoce la molestia en pocas palabras (sin disculpas largas) "
                         "y ve directo a la acción concreta. Frases muy cortas, una sola opción por vez."),
    "ansiedad": Regla("ansiedad", "Cálido y tranquilizador", 0.9,
                      "la persona está ansiosa o con miedo. Habla con calidez y calma, da pasos concretos y "
                      "menciona primero la opción más cercana o sencilla."),
    "confusion": Regla("confusion", "Lenguaje simple, confirma lo entendido", 0.95,
                       "la persona está confundida. Usa palabras simples, evita cifras de más y confirma en "
                       "una frase lo que entendiste antes de responder."),
    "normal": Regla("normal", "Estándar", 1.0, "estilo estándar: claro, cálido y breve."),
}


def decidir(estado: EstadoAfectivo) -> tuple[Regla, str]:
    """Regla vigente y su motivo, en orden de prioridad. Usa la emoción del último turno (reacciona rápido)
    y el sentimiento medio de la ventana (no salta por un turno aislado)."""
    emo = estado.ultima
    if emo is None:
        return REGLAS["normal"], "sin turnos analizados"
    medio = estado.sentimiento_medio
    señales = f" ({', '.join(emo.senales)})" if emo.senales else ""
    if emo.emocion == "urgencia":
        return REGLAS["urgencia"], f"posible emergencia{señales}"
    if emo.emocion in ("frustracion", "enojo"):
        return REGLAS["frustracion"], f"{emo.emocion} con sentimiento medio {medio:+.2f}{señales}"
    if emo.emocion in ("ansiedad", "tristeza"):
        return REGLAS["ansiedad"], f"{emo.emocion} (intensidad {emo.intensidad:.1f}){señales}"
    if emo.emocion == "confusion":
        return REGLAS["confusion"], f"confusión{señales}"
    # El promedio solo decide cuando el último turno no trae una emoción explícita (ganan las de arriba).
    if medio < -0.4:
        return REGLAS["frustracion"], f"sentimiento medio negativo {medio:+.2f}{señales}"
    return REGLAS["normal"], f"{emo.emocion}, sentimiento medio {medio:+.2f}"


@dataclass(frozen=True)
class Cambio:
    evento: ev.Adaptation
    mensajes: list[dict]  # UpdatePrompt + UpdateSpeak para Deepgram


class Politica:
    def __init__(self, voz: str):
        self.voz = voz
        self.regla = REGLAS["normal"]

    def evaluar(self, estado: EstadoAfectivo) -> Cambio | None:
        regla, motivo = decidir(estado)
        if regla is self.regla:
            return None
        anterior, self.regla = self.regla, regla
        mensajes = [{"type": "UpdatePrompt", "prompt": PREFIJO + regla.directiva}]
        if regla.velocidad != anterior.velocidad:
            mensajes.append({"type": "UpdateSpeak", "speak": {"provider": {
                "type": "deepgram", "model": self.voz, "speed": regla.velocidad}}})
        return Cambio(ev.Adaptation(rule=regla.nombre, style=regla.estilo, speed=regla.velocidad, reason=motivo),
                      mensajes)
