# Guía práctica: dónde tocar para crear cosas

Recetas cortas para el día del reto. Cada una indica qué archivo tocar y sigue los contratos de
[ARQUITECTURA.md](ARQUITECTURA.md).

---

## 0. Primeros 10 minutos del reto

1. Pega el enunciado completo en `CONTEXTO_RETO` dentro de `agents/reto.py`. Todos los agentes del
   equipo "Reto" lo reciben.
2. Decide los **roles**: ¿qué especialistas haría un equipo humano para resolver esto? Cada uno es un `Agent`.
3. Decide las **acciones**: ¿qué tiene que *hacer* el sistema (consultar una API, leer un archivo,
   calcular)? Cada acción es una tool.
4. `uv run python run.py`, elige el equipo **Reto** y prueba primero con **Agente único**. Pasa a
   Supervisor o Planner solo si hace falta.

---

## 1. Crear un agente

**Dónde:** dentro de la función del equipo en `agents/reto.py`.

```python
Agent(
    "auditor",                                   # nombre: minúsculas, sin espacios (el Router lo usa)
    "Eres auditor contable. Revisa facturas con read_file, verifica sumas con run_python "
    "y reporta inconsistencias en una tabla Markdown. " + base,  # rol + cuándo usar cada tool + formato
    tools=[read_file, run_python, *docs],
    description="Revisa facturas y detecta errores.",  # lo leen el Router, el Planner y el Supervisor
    model=model,                                 # respeta el modelo elegido en la UI
),
```

Consejos:
- La **descripción** decide si el Router lo elige, así que tiene que ser concreta.
- **Pocas tools por agente** (hasta 6). Con muchas, el modelo se confunde.
- Si el agente debe usar los documentos subidos, inclúyele `*docs`.
- Parámetros útiles: `max_steps` (límite de llamadas a tools, por defecto 10) y `temperature`.

---

## 2. Crear una herramienta (tool)

**Dónde:** si es exclusiva del reto, en `agents/reto.py`. Si es reutilizable, en `core/tools/<tema>.py`.

```python
from core.tools import tool

@tool
def buscar_cliente(cedula: str, incluir_historial: bool = False) -> str:
    """Busca un cliente por número de cédula y devuelve sus datos.

    Args:
        cedula: número de documento, solo dígitos
        incluir_historial: si True, agrega las últimas compras
    """
    try:
        datos = api.get(f"/clientes/{cedula}")          # tu API, BD, archivo...
    except Exception as e:
        return f"Error: no se pudo consultar ({e})"      # devuelve el error como texto
    return json.dumps(datos, ensure_ascii=False)        # siempre str
```

Reglas:
- **Type hints obligatorios.** Con ellos se arma el JSON Schema. Se soportan `str`, `int`, `float`,
  `bool`, `list[...]`, `Literal[...]` y `X | None`.
- **El docstring es el prompt de la tool:** la primera línea dice *qué hace* y la sección `Args:`
  describe cada parámetro.
- **Devuelve `str`.** Si es mucho texto, recórtalo: el modelo no necesita 50 000 caracteres.
- **No lances excepciones.** Devuelve `"Error: ..."` para que el modelo se corrija y reintente.
- Nombre en `snake_case` y descriptivo, porque el modelo elige la tool por su nombre y descripción.

Tools que ya existen: `read_file`, `describe_table`, `write_file` (en `core/tools/files.py`),
`web_search`, `fetch_url` (en `core/tools/web.py`), `run_python` (en `core/tools/code.py`) y
`kb.as_tool()` (búsqueda en documentos).

---

## 3. Crear un equipo nuevo (aparece en la UI)

**Dónde:** `agents/<nombre>.py` y su registro en `agents/__init__.py`.

```python
# agents/soporte.py
from core.agent import Agent
from core.memory import KnowledgeBase

def soporte_team(model: str | None = None, kb: KnowledgeBase | None = None) -> list[Agent]:
    docs = [kb.as_tool()] if kb and len(kb) else []
    return [Agent("clasificador", "...", tools=[...], model=model), ...]
```

```python
# agents/__init__.py
from agents.soporte import soporte_team
TEAMS = {"Reto": reto_team, "Soporte": soporte_team, "Equipo base": base_team}
```

---

## 4. Extraer datos estructurados (sin agente)

Cuando el reto pide *extraer*, *clasificar* o *puntuar*, muchas veces no hace falta un agente: basta
con una llamada a `structured`.

```python
from pydantic import BaseModel, Field
from core import llm

class Factura(BaseModel):
    proveedor: str
    total: float = Field(description="total en COP, sin símbolos")
    items: list[str]

factura = llm.structured(texto_del_pdf, Factura, system="Extrae los datos de la factura.")
factura.total  # float validado
```

Para usarlo dentro de un agente, envuélvelo en una tool que devuelva `factura.model_dump_json()`.

---

## 5. Elegir el modo de orquestación

| Situación del reto | Modo |
|---|---|
| Una sola tarea clara | **Agente único** (más rápido y barato) |
| Peticiones de distinto tipo (soporte, ventas, técnico) | **Router** |
| Tareas abiertas que combinan especialistas varias veces | **Supervisor** |
| Proceso por etapas fijas (investigar → analizar → redactar) | **Planner → Workers** |

Desde código, todos se usan igual:
```python
from core.orchestrator import Supervisor
respuesta = Supervisor(reto_team(model)).run("tarea...", on_step=print)
```

**Modo nuevo:** crea una clase en `core/orchestrator.py` con `name` y
`run(task, history=None, on_step=None) -> str`, y agrégala a `MODES` y a `build_runner()` en `app.py`.

---

## 6. Modelos y proveedores

- **Cambiar el modelo principal o el orden de fallback** sin tocar código, en el `.env`:
  ```env
  DEFAULT_MODEL=groq/openai/gpt-oss-120b
  FALLBACK_MODELS=groq/openai/gpt-oss-120b,gemini/gemini-3.8-flash,cohere/command-a-plus-05-2026
  ```
- **Un modelo dejó de existir:** corre `uv run python list_models.py` y actualiza `core/config.py`.
- **Proveedor nuevo:** agrega una entrada en `PROVIDERS` (`core/config.py`) con su variable de entorno
  y sus modelos en formato LiteLLM (`proveedor/modelo`), y pon la key en `.env` y `.env.example`. Luego
  `test_keys.py` debe mostrar ✅ en **tools**.
- **Un agente con un modelo distinto al de la UI:** `Agent(..., model="groq/openai/gpt-oss-20b")`.

---

## 7. Documentos y RAG

- En la UI: sube archivos en la barra lateral. Se indexan solos y los agentes que tengan `*docs`
  reciben la tool `search_documents`.
- Desde código:
  ```python
  kb = KnowledgeBase()
  kb.add_file("data/manual.pdf")
  kb.search("política de devoluciones", k=3)
  kb.save("data/kb"); kb = KnowledgeBase.load("data/kb")  # persistir entre reinicios
  ```
- Formatos soportados: PDF, DOCX, TXT, MD, CSV, XLSX y JSON. Para agregar otro, edita `extract_text`
  en `core/tools/files.py`.

---

## 8. Integraciones externas

- **API REST del reto:** créale una tool (receta 2) usando `httpx`, que ya está instalado.
- **Servidor MCP:** sigue [MCP.md](MCP.md).
- **Exponer tu solución como API:** crea `api/main.py` con FastAPI que llame a
  `Supervisor(reto_team()).run(...)`, sin duplicar lógica.

---

## 9. Depurar

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `Todos los modelos fallaron` | Cuotas agotadas o keys inválidas | `test_keys.py`; agrega otro proveedor al fallback |
| El agente no usa la tool | Descripción o instrucciones vagas | Di en las instrucciones *cuándo* usarla; mejora el docstring |
| La tool recibe argumentos raros | Type hints o `Args:` incompletos | Revisa `mi_tool.parameters` en una consola |
| Respuesta lenta | Gemini en enfriamiento o NVIDIA de principal | `DEFAULT_MODEL` de Groq |
| `_DeadlockError` al abrir la UI | Se lanzó con `streamlit run` | Usa `uv run python run.py` |
| `Acceso denegado ... .venv\lib64` | WSL y Windows comparten `.venv` | Ver la nota de Windows + WSL en el README |

Para ver qué pasa por dentro desde la consola:
```python
agente.run("...", on_step=lambda s: print(f"[{s.agent}:{s.kind}] {s.content[:200]}"))
```
