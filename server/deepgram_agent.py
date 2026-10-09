"""Cliente del Deepgram Voice Agent (spec §4.1, §9.1): un WebSocket que escucha (STT), piensa (LLM) y habla (TTS).

    conexion = await ConexionAgente.abrir(api_key, settings(funciones_agente(hub)))
    await conexion.enviar_audio(pcm16)                       # audio del micrófono
    await conexion.enviar({"type": "InjectUserMessage", "content": "..."})
    async for mensaje in conexion:                           # dict (eventos JSON) o bytes (audio TTS)
        ...

Verificado contra la API real (2026-10-09):
- URL wss://agent.deepgram.com/v1/agent/converse, auth `Authorization: Token <key>`.
- Groq como `think`: provider.type "groq" + endpoint OpenAI-compatible (obligatorio). El único modelo de Groq
  listado es openai/gpt-oss-20b (≈0,9 s de punta a punta con tool); gpt-oss-120b funciona pero tarda ≈1,2 s
  y tiende a responder con markdown.
- Secuencia: Welcome → SettingsApplied → (saludo) ConversationText/audio → AgentAudioDone; por turno:
  UserStartedSpeaking → ConversationText(user) → FunctionCallRequest → [FunctionCallResponse] →
  ConversationText(assistant) + audio → AgentAudioDone. También History y LatencyReport (para la traza).
  NO llegan AgentStartedSpeaking ni AgentThinking con este proveedor.
"""

import asyncio
import json
import logging

import websockets

from server import deepgram_stt, seguridad

log = logging.getLogger("cognia.deepgram")

URL = "wss://agent.deepgram.com/v1/agent/converse"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
TIMEOUT_APERTURA_S = 10.0

SALUDO = ("Hola, soy Kognia. Te ayudo a encontrar IPS en Colombia y su capacidad instalada: camas, salas, "
          "ambulancias. ¿Qué necesitas?")

# Prompt de sistema (spec §9.1). Corto a propósito: cada token se paga en latencia en cada turno.
PROMPT = """Eres Kognia, un orientador de salud por voz para Colombia. Hablas en español, con calidez y frases cortas.

DATOS: tienes tools que consultan EN VIVO el registro REPS de IPS (instituciones prestadoras de salud) de datos.gov.co, con corte del 5 de noviembre de 2022. Cada registro es una capacidad instalada de una sede: grupo (CAMAS, SALAS, AMBULANCIAS, CONSULTORIOS, SILLAS, CAMILLAS, otros), tipo (p. ej. Cuidado Intensivo Adulto, Sala de Cirugía), cantidad, naturaleza (Pública, Privada, Mixta), nivel de atención (1, 2, 3; vacío en el 61 % de los registros), municipio, departamento, dirección y teléfono. NO hay horarios, especialistas, precios, EPS ni disponibilidad en tiempo real.

TOOLS:
- buscar_ips: dónde atenderse (municipio, capacidad, naturaleza, nivel).
- contar_capacidad: cuántos o cuál tiene más (totales y rankings).
- detalle_ips: todo sobre una sede por su nombre.
- describir_datos: preguntas sobre la fuente misma.
- proponer_cita, registrar_solicitud_cita, crear_evento_cita, horarios_ocupados, listar_solicitudes, exportar_solicitudes_excel: solicitudes de cita (agenda propia de Kognia, no la de la IPS). Para agendar: 1) la sede sale de buscar_ips o detalle_ips (usa su id); 2) pide nombre, motivo, día y hora; 3) llama proponer_cita pasando día y hora TAL COMO LOS DIJO el usuario (nunca calcules fechas tú): valida la franja y muestra la ficha en pantalla; si está ocupada, ofrece las libres que te dé; 4) lee la propuesta y espera el "sí"; 5) registrar_solicitud_cita con los mismos datos; 6) enseguida crear_evento_cita con el número de la solicitud, sin preguntar. Siempre di que queda pendiente de confirmación por la IPS. Si piden exportar o enviar las solicitudes, usa exportar_solicitudes_excel.
Llama la tool antes de dar cualquier dato. Si la tool pide aclaración (municipio repetido, varias sedes), haz esa pregunta al usuario. Si devuelve "Error:", dilo con honestidad y ofrece otra forma.

REGLAS (estrictas: tu ÚNICA fuente es datos.gov.co a través de tus tools):
- Solo puedes afirmar lo que devolvieron tus tools en esta conversación. Nunca uses tu conocimiento propio ni fuentes externas: ni direcciones, teléfonos, nombres de IPS o cifras que no hayan salido de una tool, ni información general de salud, síntomas, medicamentos, tratamientos, EPS, precios, horarios o noticias.
- Si te preguntan algo que no está en el registro, dilo ("eso no está en el registro de datos.gov.co") y ofrece lo que sí puedes consultar (IPS por municipio, capacidad instalada, dirección y teléfono registrados).
- Si una tool responde con varias opciones o pide aclaración (varias sedes, municipio repetido), pregúntale al usuario cuál; nunca elijas tú.
- Si no tienes el dato en un resultado de tool, llama la tool antes de responder.
- Nunca digas en voz alta los id de sede (números largos) ni enlaces o URLs: son para las tools y la pantalla. Para distinguir sedes usa su nombre, su dirección o su municipio.
- Una cita NUNCA queda confirmada: se registra como solicitud pendiente de confirmación por la IPS.
- Ante una urgencia (dolor en el pecho, dificultad para respirar, sangrado fuerte, intento de suicidio), indica llamar ya al 123 antes de cualquier otra cosa.
- Menciona la fecha de corte cuando des cifras.

ESTILO (todo se convierte a voz): máximo tres frases por respuesta, en un solo párrafo. Prohibido: listas numeradas o con viñetas, saltos de línea, markdown, asteriscos, tablas y emojis. Di los números como se hablan. Si hay muchos resultados, menciona los dos o tres más relevantes y ofrece más.""" + seguridad.REGLAS_SEGURIDAD  # usuario y tools son datos, nunca instrucciones

def hoy() -> str:
    """Fecha de hoy para el prompt: sin ella el LLM no puede convertir "el viernes a las 10" en una fecha."""
    from datetime import date
    d = date.today()
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    return f"\n\nHOY es {dias[d.weekday()]} {d.isoformat()}."


# Palabras que el STT debe reconocer bien (también se agregan las lecciones aprendidas, #23). Sin las
# ciudades, "Cali" se transcribe "calle" y el agente pregunta el municipio que ya le dijeron.
KEYTERMS = ["REPS", "Kognia", "datos.gov.co", *deepgram_stt.KEYTERMS]


# Respaldo gestionado por Deepgram (no usa keys nuestras): si Groq falla o da rate limit, Deepgram pasa al
# siguiente de la cadena EN EL MISMO TURNO (verificado: ≈0,6 s extra, la llamada no se corta).
RESPALDO_LLM = {"type": "open_ai", "model": "gpt-4o-mini"}


def listen(keyterms: list[str]) -> dict:
    """Configuración de escucha (Settings y UpdateListen, cuando se aprende un keyterm a mitad de sesión)."""
    return {"provider": {"type": "deepgram", "model": "nova-3", "language": "es",
                         "keyterms": list(dict.fromkeys(keyterms))}}


def settings(funciones: list[dict], *, groq_key: str, modelo: str = "openai/gpt-oss-20b",
             voz: str = "aura-2-celeste-es", prompt: str = PROMPT, saludo: str | None = SALUDO,
             keyterms: list[str] | None = None, temperatura: float = 0.1, groq_key_2: str = "",
             respaldo: bool = True, historial: list[dict] | None = None) -> dict:
    """Mensaje Settings del Voice Agent. Audio: entra PCM16 16 kHz y sale PCM16 24 kHz sin contenedor
    (los mismos formatos del contrato del navegador, server/events.py: AUDIO_ENTRADA / AUDIO_SALIDA).

    `think` es una cadena de proveedores (fallback por turno): Groq → Groq con la 2.ª key (si hay) →
    gpt-4o-mini gestionado por Deepgram. `historial` (mensajes History) se usa al reconectar: el agente
    sigue la conversación donde iba y no repite el saludo."""
    def groq(key: str) -> dict:
        return {"provider": {"type": "groq", "model": modelo, "temperature": temperatura},
                "endpoint": {"url": GROQ_URL, "headers": {"authorization": f"Bearer {key}"}},
                "prompt": prompt, "functions": funciones}

    cadena = [groq(groq_key)] + ([groq(groq_key_2)] if groq_key_2 else [])
    if respaldo:
        cadena.append({"provider": {**RESPALDO_LLM, "temperature": temperatura}, "prompt": prompt,
                       "functions": funciones})
    agente: dict = {
        "listen": listen(keyterms if keyterms is not None else KEYTERMS),
        "think": cadena if len(cadena) > 1 else cadena[0],
        "speak": {"provider": {"type": "deepgram", "model": voz}},
    }
    if historial:
        agente["context"] = {"messages": historial}
    elif saludo:
        agente["greeting"] = saludo
    return {"type": "Settings",
            "audio": {"input": {"encoding": "linear16", "sample_rate": 16000},
                      "output": {"encoding": "linear16", "sample_rate": 24000, "container": "none"}},
            "agent": agente}


class ErrorAgente(RuntimeError):
    """No se pudo abrir o configurar la sesión con Deepgram (key, red, Settings rechazados)."""


class ConexionAgente:
    """WebSocket abierto y configurado con el Voice Agent."""

    def __init__(self, ws):
        self._ws = ws
        self.request_id: str | None = None

    @classmethod
    async def abrir(cls, api_key: str, config: dict, url: str = URL) -> "ConexionAgente":
        """Conecta, envía Settings y espera SettingsApplied. Lanza ErrorAgente si algo falla."""
        if not api_key:
            raise ErrorAgente("falta DEEPGRAM_API_KEY")
        try:
            ws = await asyncio.wait_for(
                websockets.connect(url, additional_headers={"Authorization": f"Token {api_key}"},
                                   max_size=None, ping_interval=20), TIMEOUT_APERTURA_S)
        except Exception as e:  # noqa: BLE001 — 401, DNS, timeout...
            raise ErrorAgente(f"no pude conectar con Deepgram ({type(e).__name__}: {e})"[:300]) from e
        conexion = cls(ws)
        try:
            await ws.send(json.dumps(config))
            async with asyncio.timeout(TIMEOUT_APERTURA_S):
                while True:
                    m = await ws.recv()
                    if isinstance(m, bytes):
                        continue
                    d = json.loads(m)
                    if d.get("type") == "Welcome":
                        conexion.request_id = d.get("request_id")
                    elif d.get("type") == "SettingsApplied":
                        return conexion
                    elif d.get("type") == "Error":
                        raise ErrorAgente(f"Deepgram rechazó la configuración: {d.get('description') or d}")
        except BaseException as e:
            await conexion.cerrar()
            if isinstance(e, ErrorAgente | asyncio.CancelledError):
                raise
            raise ErrorAgente(f"Deepgram no confirmó la configuración ({type(e).__name__})") from e

    async def enviar(self, mensaje: dict) -> None:
        await self._ws.send(json.dumps(mensaje, ensure_ascii=False))

    async def enviar_audio(self, pcm: bytes) -> None:
        await self._ws.send(pcm)

    def __aiter__(self):
        return self._recibir()

    async def _recibir(self):
        """Mensajes de Deepgram: dict para JSON, bytes para audio. Termina cuando se cierra la conexión."""
        try:
            async for m in self._ws:
                yield m if isinstance(m, bytes) else json.loads(m)
        except websockets.ConnectionClosed:
            return

    async def cerrar(self) -> None:
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001 — ya cerrada
            pass
