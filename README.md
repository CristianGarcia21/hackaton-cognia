# Cognia: base multi-agente para hackatón

Esqueleto listo para resolver un reto de agentes de IA. Los modelos, el fallback entre proveedores,
las herramientas, el RAG, la orquestación y la UI ya funcionan; el día del reto solo editas `agents/reto.py`.

## Documentación

| Documento | Para qué |
|---|---|
| [docs/GUIA.md](docs/GUIA.md) | Recetas: dónde tocar para crear agentes, tools, equipos, modos, RAG… |
| [docs/ARQUITECTURA.md](docs/ARQUITECTURA.md) | Diseño, capas, contratos, flujo y límites |
| [docs/MCP.md](docs/MCP.md) | Cómo conectar servidores MCP o exponer tus agentes por MCP |
| [AGENTS.md](AGENTS.md) / `CLAUDE.md` | Contexto para asistentes de IA (Claude Code, Cursor, Copilot...) |

## Arranque

```bash
uv sync                          # crea el entorno e instala dependencias
cp .env.example .env             # pon tus API keys
uv run python test_keys.py       # verifica cada key (texto, tools, embeddings)
uv run python list_models.py     # lista los modelos vigentes de cada proveedor
uv run python run.py             # abre la UI (http://localhost:8501)
```

## Estructura

```
core/
  config.py        proveedores, modelos y cadena de fallback  ← cambia modelos aquí
  llm.py           chat / ask / stream / structured (Pydantic) / embed, con fallback automático
  agent.py         Agent: instrucciones + tools + bucle de tool-calling
  orchestrator.py  Router, Supervisor, PlanExecute (misma interfaz que Agent)
  memory.py        KnowledgeBase para RAG (embeddings Gemini o palabras clave si no hay)
  tools/           @tool + read_file, describe_table, write_file, web_search, fetch_url, run_python
agents/
  reto.py          ← AQUÍ VA EL RETO
  base_team.py     equipo genérico: investigador, analista, documentos, redactor
  __init__.py      registro TEAMS (lo que aparece en la UI)
run.py             lanzador de la UI (evita un deadlock de imports en WSL)
app.py             UI Streamlit: chat, subida de archivos, selector de modelo/equipo/modo, pasos en vivo
test_keys.py       chequeo de API keys
list_models.py     modelos disponibles hoy en cada proveedor (filtro: list_models.py llama)
```

## Día del reto: receta rápida

1. Pega el enunciado en `CONTEXTO_RETO` de `agents/reto.py`.
2. Ajusta las instrucciones de los agentes y crea las tools que necesites:
   ```python
   @tool
   def buscar_cliente(cedula: str) -> str:
       """Busca un cliente por cédula.

       Args:
           cedula: número de documento
       """
       return ...
   ```
3. ¿Necesitas extraer datos con estructura? Usa `llm.structured`:
   ```python
   class Factura(BaseModel):
       total: float
       proveedor: str
   factura = llm.structured(texto_pdf, Factura)
   ```
4. Prueba en la UI con los cuatro modos y quédate con el que mejor resulte para la demo.

## Modos de orquestación

| Modo | Cuándo usarlo |
|---|---|
| Agente único | Tarea clara y un solo rol. El más rápido. |
| Router | Varias clases de petición, cada una con su especialista. |
| Supervisor | Tareas abiertas en las que hay que combinar especialistas varias veces. |
| Planner → Workers | Tareas largas por etapas (investigar → analizar → redactar). |

## Proveedores

Gemini es el principal. Groq, OpenRouter (modelos `:free`), Cerebras y Mistral tienen planes gratuitos.
Si un modelo falla (cuota, caída), `llm.chat` pasa al siguiente proveedor con key. Para cambiar el orden,
define `DEFAULT_MODEL` y `FALLBACK_MODELS` en el `.env`.
Si un modelo agota su cuota, entra en enfriamiento el tiempo que indique el proveedor y mientras tanto
se usa el siguiente. El plan gratis de Gemini permite solo ~5 peticiones/min por modelo; para una demo con
muchas llamadas conviene `DEFAULT_MODEL=groq/openai/gpt-oss-120b` (mucho más rápido y con más cuota).

Los nombres de modelo cambian seguido: si `test_keys.py` marca uno en rojo, corre `list_models.py`
y actualízalo en `core/config.py`.

> **Windows + WSL en la misma carpeta:** no pueden compartir `.venv` (uno es de Windows y otro de Linux).
> Windows usa `.venv` normal. En WSL agrega esto a `~/.zshrc` (o `~/.bashrc` con `PROMPT_COMMAND`)
> para que en discos de Windows use `.venv-linux`:
> ```zsh
> _uv_env_por_disco() { [[ $PWD == /mnt/* ]] && export UV_PROJECT_ENVIRONMENT=.venv-linux || unset UV_PROJECT_ENVIRONMENT }
> autoload -U add-zsh-hook; add-zsh-hook chpwd _uv_env_por_disco; _uv_env_por_disco
> ```
> No hace falta activar el entorno: `uv run` lo usa solo.

> `run_python` ejecuta código localmente y no es un sandbox. Sirve para la demo, pero no lo expongas a usuarios externos.
