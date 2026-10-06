# Cognia: base multi-agente para hackatón

Esqueleto listo para resolver un reto de agentes de IA. Los modelos, el fallback entre proveedores,
las herramientas, el RAG, la orquestación y la UI ya funcionan; el día del reto solo editas `agents/reto.py`.

## Arranque

```bash
uv sync                          # crea el entorno e instala dependencias
cp .env.example .env             # pon tus API keys
uv run python test_keys.py       # verifica cada key (texto, tools, embeddings)
uv run python list_models.py     # lista los modelos vigentes de cada proveedor
uv run streamlit run app.py      # abre la UI
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

> Si usas WSL y Windows a la vez, no compartan `.venv`: en Windows usa
> `$env:UV_PROJECT_ENVIRONMENT=".venv-windows"` antes de `uv run`.

> `run_python` ejecuta código localmente y no es un sandbox. Sirve para la demo, pero no lo expongas a usuarios externos.
