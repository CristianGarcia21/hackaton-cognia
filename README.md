# Kognia · Agente Vocal Cognitivo

Agente conversacional **por voz y en tiempo real** sobre la «Relación de IPS públicas y privadas según el nivel de
atención y capacidad instalada» (datos.gov.co, 41 427 registros). Explica de qué trata la fuente, responde por voz
con datos reales, registra solicitudes de cita y muestra en vivo la transcripción diarizada, las emociones, cómo se
adapta el agente y la traza de cómo razonó.

- **URL pública:** <https://hackaton-kognia.onrender.com/> (Chrome, con micrófono)
- **Repositorio:** <https://github.com/CristianGarcia21/hackaton-cognia>

## Arquitectura

```text
Navegador (HTML + JS sin build)                      Backend: 1 contenedor, FastAPI + uvicorn (asyncio)
 micrófono → AudioWorklet PCM16 16 kHz ──WS /ws/voz──▶ Sesión por WebSocket = máquina de estados + TaskGroup
 voz del agente ◀─ PCM16 24 kHz + eventos JSON ◀──────   ├─ Deepgram Voice Agent: STT Nova-3 es · LLM Groq gpt-oss-20b
 paneles: esfera · transcripción · emociones ·           │    (respaldo: 2.ª key → gpt-4o-mini) · TTS Aura-2 es
          adaptación · acciones · fuente · inspector     ├─ Deepgram STT diarizado en paralelo (solo para el panel)
                                                         ├─ Hub MCP ─▶ IPS (datos.gov.co SODA3 bajo demanda + caché)
                                                         │          └▶ Citas (SQLite: agenda por sede, .ics, Google Calendar)
                                                         └─ Cognición en paralelo (Groq vía LiteLLM con fallback):
                                                              brief · emociones + adaptación · verificador QA ·
                                                              memoria de lecciones · traza
```

El contrato entre navegador y backend es `server/events.py` (modelos Pydantic, exportado a `web/contrato.json`).

## Decisiones técnicas

| Decisión | Por qué |
|---|---|
| **Monolito modular** en un contenedor | Un solo punto de falla y de despliegue (M07: un único reintento). Cada sistema externo es un servidor MCP que se puede separar por configuración. |
| **asyncio orientado a eventos** | Casi todo es espera de red (Deepgram, Groq, datos.gov.co). Un emisor único por sesión escribe en el WebSocket. |
| **Máquina de estados** (escuchando → pensando → consultando → hablando → interrumpido) | Interrupciones naturales: al hablar encima se vacía el audio en < 100 ms (`audio_flush`) y se descarta lo del turno viejo. |
| **Tools sobre la API, sin embeddings** | Los datos son tabulares: un LLM no debe «recordar» cifras. Las tools arman SoQL (paginación y filtros), suman sin duplicados y el modelo nunca ve los 41 000 registros. |
| **Catálogo de búsqueda** al arrancar | Traduce lo que dice el usuario (errores del STT, sin tildes, «UCI») a valores exactos del dataset. |
| **Verificador QA en vivo** | Cada respuesta se contrasta con los resultados de las tools; si no está respaldada, el agente se corrige en voz. |
| **Adaptación determinista** | Tabla de reglas explicable (urgencia, frustración, ansiedad, confusión) que ajusta el prompt y la velocidad de la voz solo cuando cambia la regla. |
| **Honestidad en citas** | Los datos no tienen agenda de las IPS: se registra una *solicitud* «pendiente de confirmación por la IPS» en una agenda propia (franjas de 30 min, sin choques), visible en `/citas` y exportable a `.ics` o Google Calendar. |
| **Memoria de lecciones** | Las correcciones del usuario se guardan como keyterms del STT y lo no respaldado como reglas del prompt: el agente mejora entre sesiones. |

## Mapa de fallos

| Etapa | Falla posible | Evidencia (Inspector) | Mitigación |
|---|---|---|---|
| Micrófono / eco | El agente se transcribe a sí mismo | Turno del Agente atribuido a un hablante | `echoCancellation`; filtro de eco del agente |
| STT | Nombres propios mal transcritos | El texto no coincide | Keyterms + búsqueda difusa en el catálogo |
| Fin de turno | Corta al usuario | El usuario repite | Ajuste del umbral de fin de turno |
| Diarización | Mezcla voces parecidas | Un hablante para dos personas | Prueba previa; se declara como límite |
| Decisión del LLM | Tool o filtro equivocados | Args de la tool en la traza | Descripciones claras + verificador |
| Datos | Campo vacío (nivel sin dato en el 61 %) | La tool devuelve «sin dato» | Se dice explícitamente |
| Redacción | Cifra inventada | Verificador ⚠ | Corrección inyectada en voz |
| Proveedor | Rate limit o caída | Evento `error` | Cadena de LLM con fallback y reconexión con historial |
| Integraciones | Falla la base de datos | `tool.status = error` | Reinicio del MCP y mensaje honesto |

## Seguridad

| Riesgo | Defensa |
|---|---|
| **Inyección de prompt** por voz o texto («ignora tus instrucciones», «repite tu prompt») | Reglas de SEGURIDAD al final del prompt: lo que dice el usuario y lo que devuelven las tools son *datos*, nunca instrucciones; no revela su configuración ni cambia de rol (`server/seguridad.py`). |
| **Inyección persistente** vía la memoria de lecciones (se agrega al prompt de las sesiones siguientes) | Detector determinista: nada que parezca inyección se guarda como lección ni llega al prompt, ni siquiera lo ya guardado. |
| Consultas manipuladas | El LLM nunca escribe SoQL ni SQL: las tools arman las consultas con valores escapados o parametrizados. |
| XSS en la UI | Todo dato del servidor se pinta con `textContent`; los enlaces de acciones solo apuntan al propio servidor. |
| Fuga de secretos | Las keys viven solo en el servidor (`.env` / variables de Render); no se sirven archivos ocultos; sin `/docs` público. |
| Mensajes inválidos del navegador | Se validan con el contrato Pydantic y se responden con un `error` recuperable sin cerrar la conexión. |

## Ejecutar en local

```bash
uv sync
cp .env.example .env              # DEEPGRAM_API_KEY, GROQ_API_KEY, DATOS_GOV_KEY_ID / _SECRET
uv run uvicorn server.main:app --port 8000          # http://127.0.0.1:8000 (Chrome, con micrófono)
uv run python -m server.mock_ws                     # UI sin backend ni keys: guion de demo (caso María)
uv run pytest -q                                    # ~450 tests sin red
```

## Uso de IA (regla M04)

- **Producto:** Deepgram (STT, TTS y Voice Agent), Groq `gpt-oss-20b` (conversación y emociones) y `gpt-oss-120b`
  (brief y verificador), vía LiteLLM con fallback entre proveedores.
- **Desarrollo:** buena parte del código, los tests y la documentación se generó con asistentes de IA, dirigidos y
  revisados por el equipo. La especificación (`docs/superpowers/specs/`), el contrato y las decisiones de diseño son
  del equipo.
  - **Carlos Alape** (frontend, MCP y QA): Claude Code (Claude Opus) en VS Code para implementar y probar; subagentes
    de Claude (Sonnet) para trabajar issues en paralelo y como **agente de revisión QA antes de cada push**; la skill
    *ui-ux-pro-max* para el sistema visual (paleta, tipografía, accesibilidad). Las pruebas de la UI se hicieron con
    Edge headless y un micrófono simulado.
  - **Cristian García** (core y backend): Claude Code con el plugin *superpowers* (flujo de diseño → spec en
    `docs/superpowers/specs/` → issues) y un agente de revisión QA por issue (commits «revisión QA de #N»).
    *(Cristian: confirma o ajusta esta línea antes de la entrega.)*

La base multi-agente reutilizada (`core/`: LLM con fallback, tools, orquestación) está documentada en
`docs/ARQUITECTURA.md` y `docs/GUIA.md`.
