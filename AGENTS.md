# Contexto para asistentes de IA (Claude Code, Cursor, Copilot, Codex...)

Kognia es una base multi-agente para una hackatón cuyo reto se conoce el mismo día.
La infraestructura (LLM con fallback, agentes, orquestación, RAG, tools, UI) ya está hecha y probada.

## RETO ACTUAL: Agente Vocal Cognitivo (léelo primero)

El reto **ya se conoce** y cambia dónde se trabaja. **Antes de tocar código lee**
`docs/superpowers/specs/2026-10-09-agente-vocal-cognitivo-design.md` (diseño aprobado) y `docs/RETO.md`
(guía del equipo).

- Agente de voz en tiempo real (Deepgram Voice Agent + Groq) sobre los datos de las IPS de datos.gov.co,
  con integraciones MCP (citas en SQLite, Excel y calendario), emociones que adaptan al agente,
  verificador QA, traza por turno y despliegue público en un contenedor.
- **Código nuevo en:** `server/` (FastAPI asíncrono, sesión con máquina de estados), `mcp_servers/`,
  `web/` (HTML/JS sin compilación) y `qa/`. **No** en `agents/reto.py` ni en `app.py` (Streamlit queda
  fuera de este reto).
- **Contrato del WebSocket** entre frontend y backend: spec §6, implementado en `server/events.py`. No
  agregues eventos ni campos sin actualizar ambos.
- El único cambio permitido en `core/` es agregar variantes async a `core/llm.py` (`achat`, `astructured`).
- Prioridades P0/P1/P2 y plan por horas: spec §3 y §15. Si vas atrasado, recorta de P2 hacia P0.

Stack: Python 3.12 · uv · LiteLLM (multi-proveedor) · Streamlit · Pydantic. Idioma del proyecto: español.

## Comandos

```bash
uv sync                          # instalar (no uses pip)
uv add <paquete>                 # agregar dependencia
uv run python run.py             # UI en http://localhost:8501 (NO uses `streamlit run app.py`: deadlock en WSL)
uv run python test_keys.py       # verifica API keys, texto y tool-calling por modelo
uv run python list_models.py     # modelos vigentes de cada proveedor
```

## Mapa (dónde tocar)

| Quiero... | Archivo |
|---|---|
| Reto actual (voz, MCP, emociones, QA) | `server/`, `mcp_servers/`, `web/`, `qa/`, ver la spec |
| Retos genéricos con la UI de Streamlit | `agents/reto.py` |
| Nueva tool del reto | `server/tools/<tema>.py` con `@tool` (lógica) + `mcp_servers/<tema>.py` (solo la expone por MCP) + una línea en `server/tools_registry.py` (`registrar_servidores`) |
| Tool genérica reutilizable | `core/tools/<tema>.py` con `@tool` |
| Consultar datos.gov.co | `server/data/datos_gov.py` (cliente + caché + resolvedor); nunca descargar el dataset |
| Nuevo equipo seleccionable en la UI | `agents/<equipo>.py` + registrar en `agents/__init__.py` (`TEAMS`) |
| Cambiar/agregar modelos o proveedores | `core/config.py` (`PROVIDERS`) o `.env` (`DEFAULT_MODEL`, `FALLBACK_MODELS`) |
| Extraer datos con estructura (JSON validado) | `llm.structured(texto, MiModeloPydantic)` |
| Nuevo modo de orquestación | clase en `core/orchestrator.py` + entrada en `MODES` de `app.py` |
| Conectar un servidor MCP | ver `docs/MCP.md` (adaptador en `core/tools/mcp.py`) |
| Cambiar la UI | `app.py` |

Documentación completa: `docs/ARQUITECTURA.md` (diseño y contratos), `docs/GUIA.md` (recetas), `docs/MCP.md`.

## Contratos que NO se deben romper

- **Nadie llama a LiteLLM ni a un SDK de proveedor directamente.** Todo pasa por `core/llm.py`
  (`chat`, `ask`, `stream`, `structured`, `embed`). Ahí viven el fallback y el enfriamiento por rate limit.
- **Ejecutor** = cualquier objeto con `.name` y `.run(task, history=None, on_step=None) -> str`.
  `Agent`, `Router`, `Supervisor` y `PlanExecute` lo cumplen y son intercambiables.
- **Tool** = `core.tools.Tool` (nombre, descripción, función, JSON schema). Se crea con `@tool`;
  el schema sale de los type hints y de la sección `Args:` del docstring. **El docstring es el prompt
  que ve el modelo**: escríbelo claro.
- **Equipo** = función `(model=None, kb=None) -> list[Agent]`. Si `kb` tiene documentos, agrega `kb.as_tool()`.
- Las tools devuelven `str`. Los errores se devuelven como texto (no se lanzan) para que el modelo se corrija.
- `core/` no importa de `agents/` ni de `app.py`. La dependencia va solo hacia adentro: `app → agents → core`.

## Convenciones

- Código, nombres de agentes, prompts y mensajes en español; nombres de agentes en minúscula sin espacios.
- Instrucciones de agente: rol + qué tools usar y cuándo + formato de salida. Pocas tools por agente (≤ 6).
- No guardes keys en el código: van en `.env` (ignorado por git). Archivos generados van a `outputs/`.
- `run_python` ejecuta código local sin sandbox: no exponer a usuarios externos.
- Proveedores gratis tienen cuota baja (Gemini ≈ 5 req/min por modelo). Para pruebas intensivas usa
  `DEFAULT_MODEL=groq/openai/gpt-oss-120b`.
- Windows y WSL comparten carpeta: Windows usa `.venv`, WSL usa `.venv-linux` (ver README).
