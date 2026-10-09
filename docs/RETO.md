# Reto 01: Agente Vocal Cognitivo (guía para el equipo)

> Léelo en 5 minutos. El diseño completo está en
> [superpowers/specs/2026-10-09-agente-vocal-cognitivo-design.md](superpowers/specs/2026-10-09-agente-vocal-cognitivo-design.md).

## Qué construimos

Un agente **por voz y en tiempo real** que orienta a pacientes usando los datos reales de las IPS de
Colombia (datos.gov.co) y **actúa**: registra solicitudes de cita en una base de datos, las agenda en un
calendario y las exporta a Excel, todo mediante **servidores MCP**. En pantalla se ve la transcripción
por hablante, las emociones, **cómo se adapta el agente a esas emociones** y la **traza** de cómo razonó.

**Historia de la demo:** María dice *"Necesito una cita para una cirugía en Medellín"*. El agente busca
sedes con sala de cirugía, nota su ansiedad y le responde con calma, registra la solicitud, crea el
evento y lo deja en Excel.

## Cómo se califica (y dónde ponemos el esfuerzo)

| Peso | Criterio | Qué hacemos |
|---|---|---|
| 20 % | Voz y latencia | Deepgram Voice Agent (interrupciones incluidas) + Groq + tools sobre la API con caché |
| 15 % | Diarización | Segundo STT de Deepgram con `diarize=true` |
| 10 % | Despliegue | 1 contenedor en Railway, **desplegado desde el minuto 30** |
| 25 % | UX y demo | Paneles en vivo, preguntas pulsables, historia de María |
| ~30 % | (no publicado) fidelidad y honestidad | Solo responde con datos de las tools + verificador QA ✔/⚠ |

El evaluador además pidió: **MCP** (base de datos, Excel, calendario), **agente de QA**, que el
**sentimiento ajuste al agente**, **resiliencia** y que **sepamos explicar dónde falla**.

## Arquitectura en 30 segundos

```
Navegador ──audio/eventos── FastAPI /ws/voz ──┬── Deepgram Voice Agent (STT es · LLM Groq · TTS es)
                                              ├── Deepgram STT diarize=true (panel de transcripción)
                                              ├── capa cognitiva: emociones → adaptación, verificador, brief, traza
                                              └── Hub MCP: ips · citas (SQLite) · excel · calendario
```

- **Monolito modular asíncrono:** un servicio, asyncio y una máquina de estados por sesión. Nada de lo
  que no sea voz bloquea la voz.
- **El modelo nunca ve los 41 000 registros:** ve el *esquema* y pide datos con tools. Por eso no
  inventa y por eso es rápido.

## Quién hace qué

| Persona 1 · voz y UI (`web/`) | Persona 2 · cerebro e integraciones (`server/`, `mcp_servers/`, `qa/`) |
|---|---|
| Micrófono (AudioWorklet PCM16 16 kHz) y reproducción (24 kHz) | Puente con Deepgram (Agent + STT diarizado) |
| Vaciar el audio en `audio_flush` (interrupciones) | Cliente de datos.gov.co (bajo demanda) + MCP IPS |
| Paneles: transcripción, estado, emociones + adaptación, acciones, brief | Emociones, política de adaptación, verificador, brief |
| Inspector (traza), caja de texto de respaldo, subida de Excel/CSV | MCP citas, excel y calendario; resiliencia; agente de pruebas QA |
| Pulido visual | Despliegue y variables de entorno |

**La frontera entre los dos es el contrato del WebSocket** (spec §6), implementado en
`server/events.py`. Si necesitas un campo nuevo, **avisa y se agrega al contrato**; no lo inventes por tu
cuenta.

## Hitos (no se mueven)

| Hora | Hito |
|---|---|
| **0:30** | Contrato congelado + "hola mundo" **desplegado** en la URL pública |
| **3:00** | Demo de punta a punta desplegada: voz + IPS + diarización |
| **6:30** | Congelar funcionalidades → solo arreglos, ensayo y README |

Si vamos atrasados, se recorta en este orden: Google Calendar real → lecciones → agente de pruebas QA.
**Nunca** se recorta el despliegue ni el hito de las 3:00.

## Puesta en marcha

```bash
git clone https://github.com/CristianGarcia21/hackaton-cognia && cd hackaton-cognia
uv sync
cp .env.example .env     # pide las keys a tu compañero (nunca por el chat del repo)
```

Keys del reto: `DEEPGRAM_API_KEY` (cuenta en console.deepgram.com, unos 200 USD de crédito),
`GROQ_API_KEY` (+ una segunda key opcional), `GEMINI_API_KEY`, `DATOS_GOV_KEY_ID` + `DATOS_GOV_KEY_SECRET` (API key del portal del desarrollador
de datos.gov.co).

## Datos que hay que conocer

- **No se descarga el dataset:** las tools consultan la API de datos.gov.co cada vez (con caché).
- 41 427 filas = **sede + tipo de capacidad + cantidad** (no una fila por IPS). 15 547 sedes reales
  (id = código + número de sede); 3 145 duplicados exactos; Cali/Buenaventura cuentan como Valle.
- Capacidades: consultorios, salas (cirugía, partos, procedimientos), camas (UCI, pediatría…),
  ambulancias, camillas, unidades móviles, sillas (quimio, hemodiálisis).
- **Nivel de atención vacío en el 61 %** → el agente dice "sin dato", no inventa.
- Corte: noviembre de 2022. **No hay horarios ni agenda**: la cita es una *solicitud pendiente de
  confirmación por la IPS*.

## Guion de nuestra demo (10 min, lo ejecuta el jurado)

1. **P1–P2:** la URL abre y se ve *"Conectado a datos.gov.co · 41 427 registros · 42 páginas"*.
2. **P3:** el brief en pantalla + el saludo hablado + 3 a 5 preguntas pulsables.
3. **P4:** resumen ("¿cómo está distribuida la capacidad?"), detalle ("¿qué capacidad tiene <una IPS
   concreta>?"; elijan una real del dataset al ensayar), fuera de los datos ("¿qué horario tiene?" → "no está en los datos") y luego
   la historia de María hasta la cita, el calendario y el Excel.
4. **P5:** transcripción con 2 hablantes + panel de emociones + *"Adaptación activa: tono cálido ·
   motivo: ansiedad"*.
5. **P6:** abrir el Inspector y explicar un turno: qué contexto vio el modelo, qué tool llamó, cuánto
   tardó cada etapa, y mostrar el mapa de fallos y el reporte del agente de pruebas QA.

## Reglas del equipo

- **No llames a Groq ni a Deepgram desde otro lado** que no sea su módulo (`core/llm.py`,
  `server/deepgram_*.py`).
- Las tools devuelven texto compacto y **nunca lanzan excepciones**: devuelven `"Error: …"`.
- Todo lo que no es voz va **en paralelo** (asyncio); nada bloquea el camino crítico.
- Despliega seguido. Si algo rompe la URL, **revierte primero** y depura después.
- README (E03): declarar **qué se generó con IA** (regla M04).
