# Spec · Agente Vocal Cognitivo (Reto 01 · Kognia Labs)

| | |
|---|---|
| **Fecha** | 2026-10-09 |
| **Equipo** | 2 personas · menos de 8 horas |
| **Estado** | Diseño aprobado en conversación, pendiente de revisar el spec escrito |
| **Base** | Repositorio Kognia (`core/`: LLM con fallback, `Tool`, receta MCP en `docs/MCP.md`) |

---

## 1. Objetivo

Un **agente conversacional por voz en tiempo real** que usa como fuente principal los datos de la
«Relación de IPS públicas y privadas según nivel de atención y capacidad instalada» de datos.gov.co.
El agente **entiende la necesidad** del usuario y **actúa** sobre sistemas conectados por MCP (base de
datos, Excel y calendario). En pantalla muestra la **transcripción diarizada**, el **sentimiento y las
emociones** en vivo, **cómo se adapta** el agente a esas emociones y **la traza de cómo razonó**.

**Historia de la demo (caso María):** *"Necesito una cita para una cirugía en Medellín."* El agente
busca sedes con sala de cirugía en Medellín usando los datos reales, nota la ansiedad en la voz y
responde con más calma, registra la solicitud en la base de datos, crea el evento en el calendario y
lo deja en Excel. Todo por voz, con interrupciones naturales.

## 2. Requisitos

### 2.1 Requisitos del reto (publicados)
- **R08 · Despliegue público:** código en GitHub, URL pública, sin instalación local.
- **E01** URL pública · **E02** repo en GitHub (público o con acceso para los jueces) · **E03** README de
  una página con stack, arquitectura, decisiones y **qué se generó con IA** (regla M04).
- **M07:** no hay demo local. Si la URL falla, hay **un único reintento de 2 minutos**.
- **Guion:** P1 abrir la URL · P2 cargar la fuente · P3 brief con 3 a 5 preguntas · P4 tres preguntas
  por voz (resumen, detalle fino y una fuera de los datos) · P5 diarización y emociones en vivo ·
  P6 preguntas técnicas.

### 2.2 Requisitos inferidos (R01 a R07 no están publicados)
El documento dice "ocho requisitos" pero solo publica R08, y los pesos de calificación suman 70 %.
Inferimos del guion: (1) consumir la API con paginación o filtros, (2) indexar los datos, (3) brief con
3 a 5 preguntas, (4) STT en vivo, (5) respuesta por voz con baja latencia, (6) transcripción diarizada
con marca de tiempo y (7) panel de sentimiento y emociones en vivo. Suponemos que el 30 % sin
publicar evalúa **fidelidad y honestidad**. Hay que confirmarlo con los organizadores.

### 2.3 Lo que pidió el evaluador
- Ir **más allá de datos.gov**: conectar más sistemas por **MCP** (base de datos, Excel, calendario).
- Tener un **agente de QA**: verificador en vivo **y** agente de pruebas del sistema desplegado.
- Las métricas de sentimiento deben **ajustar cómo se comunica el agente**.
- El agente debe ser **resiliente** y **mejorar cuando se equivoca**.
- El equipo debe **entender y explicar** cómo usa el modelo el contexto y **dónde falla**.

### 2.4 Pesos de calificación
| Peso | Criterio |
|---|---|
| 20 % | Voz y latencia (STT/TTS, tiempo de respuesta, interrupciones, fluidez) |
| 15 % | Transcripción diarizada (precisión, hablantes, marcas de tiempo) |
| 10 % | Despliegue y entregable |
| 25 % | UX, diseño y demo |
| 30 % | No publicado (supuesto: fidelidad, honestidad y cognición) |

## 3. Alcance y prioridades

| Prioridad | Componente |
|---|---|
| **P0 imprescindible** | Voz con Deepgram Voice Agent (es) + interrupciones · STT diarizado · emociones por turno · ciclo de adaptación · consulta bajo demanda a datos.gov.co (cliente SODA3 + caché + catálogo de búsqueda) + tools · brief · verificador QA en vivo · traza/inspector · resiliencia N1 y N2 · MCP IPS, Citas (SQLite), Excel y Calendar **local** · caja de texto de respaldo · subida de Excel/CSV · despliegue desde la primera hora · README |
| **P1 importante** | Agente de pruebas QA en GitHub Actions (6 casos fijos) · memoria de lecciones (resiliencia N3) |
| **P2 si sobra tiempo** | Google Calendar real (misma interfaz MCP) |
| **Fuera** | Login, multiusuario persistente, UI de Streamlit, embeddings/RAG vectorial, reentrenar modelos |

**Regla de corte:** a las **3 horas** debe existir una demo de punta a punta (voz + tools de IPS +
diarización + despliegue). Todo lo demás se suma encima. Si el tiempo se acaba, se recorta de abajo
hacia arriba: primero P2 y luego P1.

## 4. Arquitectura

### 4.1 Vista general

```
┌──────────────────────────── NAVEGADOR (jurado) ─────────────────────────────┐
│  Micrófono (AudioWorklet, PCM16 16 kHz, echoCancellation)   Reproducción TTS │
│  Paneles: Brief+preguntas · Transcripción diarizada · Emociones+Adaptación  │
│           Acciones (citas/Calendar/Excel) · Inspector (traza) · Estado       │
└───────────────────────────────▲──────┬──────────────────────────────────────┘
                 eventos JSON +  │      │ audio PCM16 (binario) + mensajes JSON
                 audio TTS (bin) │      ▼        WebSocket /ws/voz
┌──────────────── BACKEND · 1 contenedor · 1 URL HTTPS (Railway) ──────────────┐
│ FastAPI (1 worker, asyncio)                                                  │
│  Sesión (una por WebSocket) = máquina de estados + grupo de tareas + colas   │
│   T1 receptor de audio ──┬─▶ T2 Deepgram Voice Agent  (STT·LLM Groq·TTS)     │
│                          └─▶ T3 Deepgram STT diarize=true (solo panel)       │
│   T4 ejecutor de tools ◀─ FunctionCallRequest ─ T2 ; ─▶ Hub MCP (await)      │
│   T5 emociones → política de adaptación → UpdatePrompt / UpdateSpeak         │
│   T6 verificador QA → veredicto (+ InjectAgentMessage si corrige)            │
│   T7 emisor único de eventos al navegador                                    │
│  Capa cognitiva: brief · emociones · adaptación · verificador · lecciones ·  │
│                  traza  (core/llm.py async + Groq)                           │
│  Hub MCP (clientes async, subprocesos stdio lanzados al arrancar):           │
│   mcp_servers/ips.py · citas.py (SQLite) · excel.py · calendario.py          │
└──────┬───────────────────────────────────────────────────────────────────────┘
       ▼ bajo demanda: cada tool consulta SODA3 (caché de resultados + catálogo de búsqueda)
   API datos.gov.co (SODA3 con API key; sin archivo de respaldo)
   GitHub Actions: agente de pruebas QA → modo texto contra la URL pública → reporte
```

**Principio:** Deepgram se ocupa de la voz; Kognia del razonamiento, la adaptación, la verificación y
las integraciones. Cada sistema externo es un servidor MCP que se puede separar por configuración
(`stdio` → `http`).

### 4.2 Por qué un monolito modular
- **Latencia:** las tools corren en subprocesos locales (milisegundos), sin saltos de red.
- **Estabilidad (10 % + un solo reintento):** un despliegue tiene un solo punto de falla.
- **Tiempo:** un solo pipeline de despliegue.
- **Extensibilidad:** cambiar `MCPConnection.stdio("mcp_servers/citas.py")` por
  `MCPConnection.http("https://…/mcp")` separa un servicio sin tocar el agente.

### 4.3 Estructura de carpetas (nueva, junto a la base Kognia)

```
server/
  main.py              FastAPI: rutas HTTP, /ws/voz, arranque (dataset, MCP, brief)
  config.py            variables de entorno del reto (keys, modelos, voz)
  session.py           Sesión: máquina de estados + grupo de tareas + colas
  states.py            enum Estado y transiciones válidas
  events.py            modelos Pydantic de TODOS los eventos del WebSocket (contrato)
  deepgram_agent.py    cliente del Voice Agent (Settings, Update*, Inject*, eventos)
  deepgram_stt.py      cliente STT con diarización
  tools_registry.py    definiciones de funciones para el Voice Agent ↔ tools MCP
  mcp_hub.py           clientes MCP async (abre, supervisa y reinicia subprocesos)
  cognition/
    brief.py           estadísticas del dataset + brief y preguntas (LLM)
    emotions.py        análisis de emociones por turno (LLM estructurado)
    adaptation.py      política de adaptación (tabla de reglas determinista)
    verifier.py        verificador QA en vivo
    lessons.py         memoria de lecciones (SQLite)
    trace.py           traza por turno (spans, tiempos, contexto)
  data/
    datos_gov.py       cliente SODA3 bajo demanda + caché + catálogo/resolvedor de búsqueda
server/tools/           tools del reto con el contrato @tool de core/tools (la lógica)
  ips.py               describir_datos, buscar_ips, contar_capacidad, detalle_ips
mcp_servers/           solo exponen esas tools por MCP (en proceso o stdio), sin lógica
  ips.py · citas.py · excel.py · calendario.py
web/
  index.html · app.js · audio-worklet.js · styles.css
qa/
  casos.yaml · tester.py
.github/workflows/qa.yml
Dockerfile · railway.json (o equivalente)
```

`core/` se reutiliza: `core/tools` (`Tool`, `@tool`) y `core/llm.py`, al que **se agregan variantes
async** (`achat`, `astructured`) con `litellm.acompletion` y el mismo fallback y enfriamiento. Es el
único cambio en `core/`. La UI de Streamlit (`app.py`) queda fuera de este reto.

## 5. Concurrencia y máquina de estados

### 5.1 Modelo
**asyncio orientado a eventos**, no secuencial ni basado en hilos. Casi todo es espera de red
(WebSockets, Groq, Deepgram), y un event loop atiende muchas esperas sin bloquear. El trabajo de CPU
(filtros de pandas, búsqueda difusa, escribir xlsx) va en `asyncio.to_thread`. Los clientes MCP se usan
con `await` directamente, **sin** el puente de hilos de `docs/MCP.md`, que era para el `Agent` síncrono.

### 5.2 Camino crítico frente a trabajo en paralelo
- **Camino crítico** (objetivo < 1,5 s hasta el primer audio): fin de turno → LLM decide → tool →
  LLM responde → primer audio TTS.
- **En paralelo** (nunca bloquea la voz): diarización, emociones, verificador, eventos de acciones y
  traza.

### 5.3 Tareas por sesión
| Tarea | Responsabilidad |
|---|---|
| T1 receptor | Lee los frames de audio del navegador y los duplica a T2 y a T3 |
| T2 Voice Agent | WebSocket con Deepgram Agent: envía audio, recibe eventos y audio TTS |
| T3 STT diarizado | WebSocket con Deepgram Listen `diarize=true`: transcripciones por hablante |
| T4 ejecutor de tools | Atiende `FunctionCallRequest`, llama al MCP con tiempo límite y responde |
| T5 emociones/adaptación | Por cada turno final de T3: emoción → política → `UpdatePrompt`/`UpdateSpeak` |
| T6 verificador | Por cada respuesta del agente: veredicto; si corresponde, corrección |
| T7 emisor | Única tarea que escribe en el WebSocket del navegador (`asyncio.Queue`) |

Se usa `asyncio.TaskGroup`; si se cierra la conexión se cancela todo el grupo.

### 5.4 Máquina de estados
Estados: `INACTIVO`, `ESCUCHANDO`, `PENSANDO`, `EJECUTANDO_TOOL`, `HABLANDO`, `INTERRUMPIDO`.

| Evento (de Deepgram o interno) | Transición |
|---|---|
| `UserStartedSpeaking` | INACTIVO/HABLANDO → ESCUCHANDO (si venía de HABLANDO, pasa por **INTERRUMPIDO**) |
| `AgentThinking` / fin de turno | ESCUCHANDO → PENSANDO |
| `FunctionCallRequest` | PENSANDO → EJECUTANDO_TOOL |
| respuesta de la tool enviada | EJECUTANDO_TOOL → PENSANDO |
| `AgentStartedSpeaking` | PENSANDO → HABLANDO |
| `AgentAudioDone` | HABLANDO → INACTIVO |
| `FunctionCallCancelled` | EJECUTANDO_TOOL → ESCUCHANDO (el resultado se descarta) |

**En INTERRUMPIDO:** se envía `audio_flush` al navegador, se cancela el verificador del turno a
medias y se descartan los frames de audio viejos. Cada turno lleva un `turn_id`; los resultados de un
turno viejo no se hablan y se muestran en su lugar. El estado se envía a la UI (indicador "escuchando /
pensando / consultando IPS… / hablando").

> Los nombres exactos de los eventos de Deepgram se confirman contra
> `developers.deepgram.com/docs/voice-agent-outputs` durante la implementación.

## 6. Contrato del WebSocket `/ws/voz` (frontera entre persona 1 y persona 2)

**Navegador → backend**
- Frames **binarios**: audio PCM16 LE, mono, 16 kHz, en trozos de unos 20 a 40 ms.
- JSON `{"type":"start"}` · `{"type":"stop"}` · `{"type":"text_input","text":"..."}` (respaldo por texto).

**Backend → navegador** (JSON con `type`, salvo el audio TTS, que va en frames **binarios** PCM16 24 kHz)

| `type` | Campos | Uso en la UI |
|---|---|---|
| `ready` | `session_id`, `voice`, `sources` | Habilitar el micrófono |
| `state` | `state`, `turn_id` | Indicador de estado |
| `audio_flush` | `turn_id` | Vaciar el búfer de reproducción (interrupción) |
| `transcript` | `segment_id`, `turn_id?`, `speaker` ("Hablante 1"…/"Agente"), `text`, `start?`, `end?`, `is_final` | Panel de transcripción (los parciales y el final comparten `segment_id`) |
| `agent_text` | `turn_id`, `text` | Texto de lo que dice el agente |
| `emotion` | `turn_id?`, `speaker`, `sentiment` (−1..1), `emotion`, `intensity` (0..1), `signals[]` | Panel de emociones |
| `adaptation` | `active` (derivado: `rule != normal`), `style`, `speed`, `reason`, `rule` | Indicador de adaptación |
| `verification` | `turn_id`, `status` (respaldado / parcial / no_respaldado / fuera_de_datos_ok), `issues[]`, `correction?` | Marca ✔/⚠ en la respuesta |
| `tool` | `turn_id`, `name`, `args`, `status` (running/ok/error/timeout/cancelled), `ms`, `summary` | Inspector y acciones |
| `action` | `kind` (cita/evento/excel), `data`, `link?` | Panel de acciones |
| `trace` | `turn_id`, `spans[]` (`stage`, `ms`, `detail`), `context` | Inspector (cascada de tiempos) |
| `brief` | `summary`, `key_points[]`, `questions[]` (3 a 5), `stats` | Panel del brief |
| `source_status` | `source`, `status`, `rows` (acumulado), `total_rows?`, `pages`, `progress` | Animación de carga (P2) |
| `lesson` | `kind`, `content`, `origin` | Panel de lecciones |
| `error` | `where`, `message`, `recoverable` | Aviso visible y no bloqueante |

**HTTP:** `GET /` (frontend) · `GET /api/health` · `GET /api/brief` · `POST /api/fuentes` (subir
Excel/CSV) · `GET /api/citas/excel` (descarga) · `GET /api/calendario.ics`.

`server/events.py` define cada evento como un modelo Pydantic. Es la **fuente de verdad** del contrato
(si esta tabla y el código difieren, manda el código). El frontend lo consume desde `web/contrato.json`
(esquemas, enums y ejemplos), regenerado con `uv run python -m server.events --exportar`. Los valores que
vienen de LLMs o relojes se normalizan/recortan en vez de rechazarse; los mensajes inválidos del cliente se
responden con `error` recuperable sin cerrar la conexión. `?` = opcional (no se envía si no hay valor).

## 7. Datos: IPS de datos.gov.co

### 7.1 Hechos verificados (2026-10-09)
- 41 427 filas · 9 320 prestadores · **15 547 sedes reales**. **Cada fila = sede + tipo de capacidad +
  cantidad.** El id de una sede es `c_digo_sede` + `n_mero_sede` (el código solo NO es único).
- Campos: `departamento`, `municipio`, `c_digo_prestador`, `nombre_prestador`, `nit_ips`,
  `naturaleza`, `num_nivel_atencion`, `c_digo_sede`, `n_mero_sede`, `nom_sede_ips`, `gerente`,
  `direcci_n`, `email`, `tel_fono`, `nom_grupo_capacidad` (7 grupos: CONSULTORIOS, SALAS, CAMAS,
  AMBULANCIAS, CAMILLAS, UNIDAD MOVIL, SILLAS), `nom_descripcion_capacidad` (63 tipos, p. ej. "Sala de
  Cirugía", "Quirófano", "Intensiva Adultos"), `num_cantidad_capacidad_instalada`, `fecha_corte` (nov. 2022).
- **Calidad:** 3 145 filas son **duplicados exactos** (sobre todo ambulancias): las sumas se hacen
  deduplicando en la propia consulta. El REPS reporta **5 distritos como "departamento"** (Barranquilla,
  Cali, Buenaventura, Cartagena, Santa Marta): "Valle del Cauca" debe incluir Cali y Buenaventura.
  Con eso quedan los 33 departamentos reales.
- `num_nivel_atencion` está **vacío en el 62 %** de las filas. Se informa siempre como "sin dato".
- **No hay** horarios, disponibilidad, especialistas ni agenda.

### 7.2 Acceso bajo demanda (`server/data/datos_gov.py`) — NO se descarga ni se guarda el dataset
Las tools **consultan la API cada vez que se necesita**. Nunca se lee un archivo con los datos crudos.

1. **`ClienteDatosGov.consultar(soql)`**: POST a **SODA3**
   `https://www.datos.gov.co/api/v3/views/s2ru-bqt6/query.json` con cuerpo
   `{"query": "<SoQL>", "includeSynthetic": false, "page": {"pageNumber": 1, "pageSize": N}}` y auth
   **Basic** con la API key (`DATOS_GOV_KEY_ID` / `DATOS_GOV_KEY_SECRET`). Conexión reutilizada
   (keep-alive), timeout de 3 s por intento, un reintento ante 5xx/red. Si la key es rechazada sigue
   sin auth (la API lo permite con límite de uso). Si no responde → `FuenteNoDisponible` (la tool lo
   dice con honestidad; **no hay archivo de respaldo**). Un 400 → `ConsultaInvalida` (bug de la tool).
2. **Caché de resultados** (clave = SoQL): TTL de 1 h (los datos tienen corte 2022 y no cambian), límite
   de tamaño LRU y deduplicación de consultas idénticas simultáneas. Las preguntas repetidas (p. ej. las
   sugeridas del brief) responden en ~0 ms. Latencia medida sin caché: 230–350 ms (la primera ~1 s).
3. **Catálogo de búsqueda** (`cargar_catalogo`, al arrancar, ~0,7 s, 3 consultas en paralelo): solo
   **valores de referencia** traídos de la API con `GROUP BY` — 1 113 pares municipio/departamento y
   63 tipos de capacidad — más el total de registros. Sirve para saber **qué consultar**:
   - `resolver_departamento("el Valle")` → `departamento IN ('Buenaventura','Cali','Valle del cauca')`
   - `resolver_municipio("Medeyin")` → `MEDELLÍN` (búsqueda difusa con `rapidfuzz`, sin tildes)
   - `resolver_capacidad("UCI pediátrica")` → los tipos intensivos pediátricos (sinónimos + modificadores)
   - Si no hay coincidencia devuelve **sugerencias** ("¿quisiste decir…?").
   Los nombres de IPS **no** se cachean: se buscan en vivo con `upper(nom_sede_ips) like '%…%'`.
4. **SoQL seguro**: lo arman las tools con `texto()`/`en()` (literales escapados). **El LLM nunca
   escribe SoQL** (decisión A: solo tools tipadas).
5. Se emite `source_status` (`conectando` → `listo` con el total de registros, o `error`), narrativa del
   paso P2; si la API no responde al arrancar, el servidor sigue vivo (`/api/health` "degradado") y
   reintenta cada 15 s.

### 7.3 Por qué no usamos embeddings
Las preguntas son de **filtrar, contar y sumar** sobre una tabla; la similitud semántica no sabe sumar.
Usamos **tools tipadas** y **búsqueda difusa** (`rapidfuzz`) para corregir los nombres que el STT
transcribe mal.

## 8. Tools y servidores MCP

Las funciones se declaran en `agent.think.functions` **sin `endpoint`**, así que se ejecutan en el
cliente, es decir, en nuestro backend. El backend las despacha al MCP correspondiente.

| Servidor MCP | Tool | Firma (resumen) | Devuelve |
|---|---|---|---|
| **ips** | `describir_datos` | `()` | Esquema, totales, cobertura, fecha de corte |
| | `buscar_ips` | `(municipio?, departamento?, capacidad?, naturaleza?, nivel?, limite=5)` | Top sedes: nombre, municipio, dirección, teléfono, naturaleza, capacidad relevante |
| | `contar_capacidad` | `(agrupar_por, departamento?, municipio?, capacidad?, naturaleza?)` | Agregados (sumas y conteos de sedes) |
| | `detalle_ips` | `(nombre, municipio?)` | Sede con coincidencia difusa + todas sus capacidades; o "¿quisiste decir…?" |
| **citas** (SQLite) | `registrar_solicitud_cita` | `(paciente, documento?, telefono?, sede_codigo, motivo, fecha_preferida?)` | id de la solicitud, estado "pendiente de confirmación por la IPS" |
| | `listar_solicitudes` | `(paciente?)` | Solicitudes registradas |
| **excel** | `exportar_solicitudes_excel` | `()` | Ruta y enlace de descarga del .xlsx |
| | `consultar_fuente_adicional` | `(pregunta_o_filtro, hoja?)` | Filas del Excel/CSV subido |
| **calendario** | `crear_evento_cita` | `(solicitud_id, fecha_hora, duracion_min=30)` | Evento con enlace (.ics local o Google) |

**Reglas de las tools:** resultados **compactos** (top 5 y campos clave), errores devueltos como texto,
tiempo límite de **3 s** por llamada; las tools de IPS consultan la API con SoQL armado por ellas (caché
de resultados en `ClienteDatosGov`) y deduplican al sumar. Las tools
son "gruesas": una sola llamada debe bastar para la mayoría de las preguntas, porque cada vuelta extra
al LLM cuesta entre 200 y 400 ms.

**Honestidad en el agendamiento:** los datos no tienen agenda. La cita se registra como **solicitud
pendiente de confirmación por la IPS**, y el evento de calendario se titula igual. El agente lo dice.

**Calendario:** la interfaz MCP es única y tiene dos implementaciones: **local** (P0: SQLite + .ics +
vista en la UI) y **Google Calendar** con cuenta de servicio (P2). Se elige por variable de entorno.

## 9. Capa cognitiva

### 9.1 Contexto que ve el modelo
| Capa | Contenido |
|---|---|
| Prompt de sistema | Rol (orientador de salud para Colombia), reglas de honestidad ("si no está en los datos, dilo"; "nunca inventes cifras"; urgencias → 123), **esquema resumido del dataset** (campos, valores posibles, fecha de corte, nivel vacío en el 61 %), instrucciones de uso de cada tool, estilo para voz (frases cortas, sin tablas ni markdown) |
| Adaptación afectiva | Directivas agregadas con `UpdatePrompt` (sección 9.3) |
| Lecciones | Sinónimos y reglas aprendidas (sección 10.3) |
| Historial | Lo mantiene Deepgram durante la sesión |
| Resultados de tools | Solo lo que devuelve la consulta, compacto. **El modelo nunca ve los 41 000 registros.** |

El LLM del Voice Agent es Groq (`openai/gpt-oss-120b`; `gpt-oss-20b` si hace falta más velocidad).
La configuración exacta de `think.provider` y `endpoint` para Groq se confirma en
`developers.deepgram.com/docs/voice-agent-llm-models`.

### 9.2 Emociones (`cognition/emotions.py`)
Por cada turno **final** del STT diarizado, `astructured` (Groq `gpt-oss-20b`) produce
`Emocion{sentimiento: float [-1,1], emocion: Literal[alegria, calma, neutral, confusion, ansiedad,
frustracion, enojo, tristeza, urgencia], intensidad: float [0,1], senales: list[str]}`. El estado
afectivo de la sesión es el promedio móvil de los últimos 3 turnos del usuario más su tendencia. Se
emite `emotion` y nunca bloquea la voz.

### 9.3 Política de adaptación (`cognition/adaptation.py`)
Es una **tabla de reglas determinista**, explicable. Se evalúa después de cada análisis de emoción;
**solo** envía `UpdatePrompt`/`UpdateSpeak` cuando cambia la regla activa. Aplica **desde el siguiente
turno**.

| Regla | Condición | Estilo (`UpdatePrompt`) | Velocidad | Intención |
|---|---|---|---|---|
| `urgencia` | emoción = urgencia, o señales de riesgo vital | Calmado y firme | 0,95 | Recomendar urgencias o la línea 123 **antes** de cualquier trámite |
| `frustracion` | sentimiento < −0,4 o frustración/enojo | Frases cortas, reconocer la molestia | 1,0 | Ir directo a la acción concreta |
| `ansiedad` | ansiedad o miedo | Cálido, tranquilizador | 0,9 | Pasos concretos, la opción más cercana primero |
| `confusion` | confusión o repregunta | Lenguaje simple, sin cifras de más | 0,95 | Confirmar lo que entendió antes de responder |
| `normal` | resto | Estándar | 1,0 | Respuesta completa |

Como `UpdatePrompt` **agrega** texto al prompt, cada directiva empieza así: *"AJUSTE DE ESTILO VIGENTE
(reemplaza cualquier ajuste de estilo anterior): …"*. Se emite `adaptation` con la regla y el motivo.

### 9.4 Verificador QA en vivo (`cognition/verifier.py`)
Al terminar cada respuesta del agente: `astructured(pregunta del usuario, tools llamadas con sus
resultados, respuesta)` → `Veredicto{estado: respaldado|parcial|no_respaldado|fuera_de_datos_ok,
problemas: list[str], correccion: str|None}`. Corre en paralelo y se cancela si hay interrupción. Si el
veredicto es `no_respaldado` y hay corrección, y la sesión está INACTIVA, envía `InjectAgentMessage`
("Corrijo lo anterior: …") y registra una lección. Se emite `verification`.

### 9.5 Brief (`cognition/brief.py`)
Al arrancar, después de cargar los datos: se calculan estadísticas reales (totales, top departamentos,
distribución por grupo, cobertura del nivel, fecha de corte) y el LLM produce
`Brief{resumen, puntos_clave, preguntas_sugeridas (3 a 5)}`. Se guarda en caché. El saludo hablado del
agente (`agent.greeting`) es una versión de 2 frases del brief. Las preguntas sugeridas se pueden
pulsar en la UI (se envían como `text_input`).

## 10. Resiliencia y mejora

### 10.1 Nivel 1 · corrección dentro de la conversación
| Error | Detección | Corrección |
|---|---|---|
| Nombre mal transcrito | Sin coincidencia exacta en la tool | Búsqueda difusa → "¿quisiste decir…?" y el agente confirma |
| Parámetros incorrectos | Error de la tool como texto | El LLM reintenta |
| Afirmación no respaldada | Verificador ⚠ | `InjectAgentMessage` con la corrección |
| El usuario corrige | Siguiente turno | El agente rehace la consulta |

### 10.2 Nivel 2 · recuperación ante fallos
| Falla | Recuperación |
|---|---|
| Groq con rate limit o caído | `UpdateThink` → segunda key de Groq (`GROQ_API_KEY_2`) o Gemini, sin cortar |
| Se cae el WebSocket del Voice Agent | Reconexión con espera creciente + `History` (conversación y llamadas a funciones) |
| Se cae un servidor MCP | `mcp_hub` reinicia el subproceso; mientras tanto la tool responde "no disponible, reintento" |
| Una tool tarda más de 3 s | Timeout → el agente lo dice y ofrece una alternativa |
| La API de datos falla al arrancar | El servidor sigue vivo ("degradado") y reintenta cada 15 s; las tools dicen "datos.gov.co no responde" |
| Falla el micrófono o la voz | Caja de texto → `InjectUserMessage`, con los mismos paneles |
| Silencios largos | `KeepAlive` periódico |

### 10.3 Nivel 3 · memoria de lecciones (P1)
Tabla `lecciones(id, tipo: keyterm|sinonimo|regla, contenido, origen, creada)` en SQLite. Se alimenta
de: ⚠ del verificador, correcciones del usuario, "¿quisiste decir…?" confirmados y fallos del agente de
pruebas. Al iniciar la sesión: los `keyterm` van a la configuración de escucha (o a `UpdateListen`) y
los `sinonimo`/`regla` se agregan al prompt. Se emite `lesson`. **No es reentrenamiento:** es mejora del
contexto entre sesiones, y así se explica en P6.

## 11. Observabilidad: traza y mapa de fallos

### 11.1 Traza por turno (`cognition/trace.py`)
Cada turno registra spans con su tiempo: `fin_turno`, `llm_decide`, `tool:<nombre>` (con args y
tamaño del resultado), `llm_redacta`, `primer_audio`, `verificador`, `emocion`, `adaptacion`. Se emite
`trace` a la UI (panel **Inspector** plegable, con cascada de tiempos y contexto usado) y se guarda en
JSONL por sesión (`outputs/trazas/`). Los tiempos se miden con los eventos de Deepgram y con relojes
del backend.

### 11.2 Mapa de fallos (va también al README)
| Etapa | Falla posible | Evidencia en la traza | Mitigación |
|---|---|---|---|
| Micrófono/eco | El agente se transcribe a sí mismo | Turno del "Agente" atribuido a un hablante humano | `echoCancellation`; los turnos del agente se etiquetan por sus eventos |
| STT | Nombres propios mal transcritos | El texto no coincide | Keyterms + búsqueda difusa |
| Fin de turno | Corta al usuario | El usuario repite | Ajustar el umbral de fin de turno |
| Diarización | Mezcla voces parecidas | Un solo hablante para dos personas | Prueba previa; se declara como límite |
| Decisión del LLM | Tool o filtro equivocados | Args del span de la tool | Descripciones claras + verificador |
| Datos | Campo vacío (nivel en el 61 %) | La tool devuelve "sin dato" | Se dice explícitamente |
| Redacción | Cifra inventada | Verificador ⚠ | Corrección inyectada |
| Proveedor | Rate limit o caída | Span de error | `UpdateThink` / `History` |
| Integraciones | Falla la base de datos o el calendario | `tool.status=error` | Reinicio del MCP, implementación local, mensaje honesto |

## 12. Agente de pruebas QA (P1)
- `qa/casos.yaml`: 6 casos: resumen nacional, detalle de una IPS con un nombre mal escrito, conteo por
  departamento, **pregunta fuera de los datos**, flujo de cita completo y un mensaje con frustración
  (que debe activar la adaptación).
- `qa/tester.py`: abre `/ws/voz` en la **URL pública** en modo texto (`text_input`), recoge
  `agent_text`, `verification`, `adaptation`, `tool` y `trace`, y evalúa con un LLM juez y una rúbrica
  (fidelidad, honestidad, uso correcto de las tools, latencia). Los fallos se registran como lecciones.
- `.github/workflows/qa.yml`: corre en `push` a main y de forma manual, con el secreto `PUBLIC_URL`.
  Publica el reporte en Markdown como *job summary* y como artefacto.

## 13. Frontend (`web/`)
HTML + JS sin compilación + Tailwind por CDN, servido por FastAPI. Captura con **AudioWorklet** a PCM16
16 kHz (`echoCancellation`, `noiseSuppression`, `autoGainControl`). Reproducción con una cola en
`AudioContext` a 24 kHz que se **vacía al recibir `audio_flush`**.

Layout de escritorio: columna izquierda con el brief y las preguntas, centro con la transcripción
diarizada (un color por hablante y marca de tiempo `mm:ss`), derecha con emociones (gráfico de línea de
sentimiento + emoción actual + indicador de adaptación) y acciones. Debajo, el Inspector plegable y la
caja de texto de respaldo. Indicador de estado grande en la parte superior. Aviso de "Usa Chrome y
permite el micrófono".

## 14. Despliegue
- **Railway** (o similar con Docker, WebSockets y sin suspensión): un contenedor `python:3.12-slim` con
  uv, `uvicorn server.main:app --workers 1`, HTTPS (el navegador exige HTTPS para el micrófono).
- `/api/health` es **liveness**: responde 200 mientras el proceso vive (cuerpo `ok` o `degradado`) y es el
  healthcheck de Railway, así un componente lento no impide el deploy. `/api/ready` es **readiness**: 200 solo
  cuando el dataset, los MCP y el brief están listos. Smoke test de cualquier URL: `uv run python scripts/smoke.py <url>`.
- **Desplegar un "hola mundo" en la primera media hora** y luego en cada avance.
- Variables de entorno: `DEEPGRAM_API_KEY`, `GROQ_API_KEY`, `GROQ_API_KEY_2` (opcional),
  `GEMINI_API_KEY`, `DATOS_GOV_KEY_ID`, `DATOS_GOV_KEY_SECRET`, `CALENDAR_BACKEND=local|google`, `GOOGLE_SERVICE_ACCOUNT_JSON`
  (P2), `PUBLIC_URL` (para QA).
- Repo: **público**, o con acceso para los jueces (E02).

## 15. Plan de trabajo (2 personas, menos de 8 horas)

| Bloque | Persona 1 · voz y UI | Persona 2 · cerebro e integraciones |
|---|---|---|
| **0:00–0:30** | Juntos: revisar el contrato (§6) → `server/events.py`; esqueleto FastAPI + `web/index.html` **desplegado** en Railway | ← lo mismo |
| **0:30–3:00** | Captura y reproducción de audio, WS, paneles de transcripción y estado, `audio_flush` | Loader de datos + MCP IPS + `deepgram_agent.py` (Settings, functions, reenvío de eventos) + `deepgram_stt.py` |
| **3:00** | **Hito: demo de punta a punta desplegada** (voz + IPS + diarización) | ← |
| **3:00–5:00** | Paneles de emociones, adaptación, verificación, acciones y brief; preguntas pulsables | Emociones + política de adaptación + verificador + brief + MCP Citas/Excel/Calendario local + máquina de estados completa |
| **5:00–6:30** | Inspector (traza), pulido visual, caja de texto de respaldo, subida de Excel/CSV | Resiliencia N2 (UpdateThink, reconexión, reinicio de MCP) + traza + agente de pruebas QA + lecciones (P1) |
| **6:30–7:30** | Juntos: ensayo de la demo con dos voces, ajuste del umbral de fin de turno, README (E03), respaldo de la URL | ← |

**Puntos de sincronización:** en el minuto 0:30 (contrato congelado), a las 3:00 (demo de punta a punta)
y a las 6:30 (congelar funcionalidades).

## 16. Riesgos
| Riesgo | Mitigación |
|---|---|
| Diarización imperfecta con un solo micrófono | Probar en la primera hora con dos voces; declararlo como límite |
| Rate limit de Groq durante la demo | Segunda key + `UpdateThink` |
| Se acaba el crédito de Deepgram | Monitorear el panel; la demo consume centavos |
| Micrófono o navegador del jurado | HTTPS, aviso de Chrome, respaldo por texto |
| La API de datos.gov falla en la demo | Caché de resultados (las preguntas sugeridas se precalientan) + mensaje honesto; reintento 1× por consulta |
| Formato exacto de los mensajes de Deepgram | Verificar contra la documentación oficial al implementar (marcado en §5.4, §9.1) |
| No alcanza el tiempo | Cortes en el orden P2 → P1; hito a las 3:00 |

## 17. Trazabilidad: criterios → diseño
| Criterio / paso | Dónde se cumple |
|---|---|
| Voz y latencia (20 %) | §4, §5, §8 (tools gruesas, caché), Voice Agent con interrupciones |
| Diarización (15 %) | T3 STT `diarize=true`, evento `transcript`, §11.2 |
| Despliegue (10 %) | §14 |
| UX y demo (25 %) | §13, historia de María, Inspector, preguntas pulsables |
| Fidelidad y honestidad (≈30 %) | §7.3, §9.1, §9.4, §8 (honestidad en el agendamiento) |
| P2 fuente | `source_status` (conexión a la API + total de registros) + subida de Excel/CSV |
| P3 brief | §9.5 |
| P4 resumen / detalle / fuera de datos | `contar_capacidad` / `detalle_ips` + búsqueda difusa / reglas + verificador |
| P5 | Eventos `transcript`, `emotion` y `adaptation` |
| P6 | §4.2, §5, §9.1, §11 (traza y mapa de fallos), reporte de QA |
| Evaluador: MCP (BD/Excel/Calendar) | §8 |
| Evaluador: agente de QA | §9.4 + §12 |
| Evaluador: adaptación por sentimiento | §9.3 |
| Evaluador: resiliencia y mejora | §10 |
| Evaluador: entender el proceso | §9.1 + §11 |
