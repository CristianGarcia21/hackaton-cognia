"""==========  AQUÍ VA EL RETO DE LA HACKATÓN  ==========

Pasos para adaptarlo en minutos:
1. Cambia las instrucciones de los agentes según el reto (o crea agentes nuevos).
2. Si necesitas una herramienta propia (una API, una base de datos...), créala con @tool.
3. Elige "Reto" en el selector de equipo de la UI y prueba los modos (agente único, router...).

Todo lo demás (modelos, fallback, RAG, UI) ya funciona solo.
"""

from core.agent import Agent
from core.memory import KnowledgeBase
from core.tools import tool
from core.tools.code import run_python
from core.tools.files import read_file, write_file
from core.tools.web import web_search

CONTEXTO_RETO = """
(Pega aquí la descripción del reto, reglas, datos del cliente, criterios de evaluación...)
"""


# --- Ejemplo de herramienta propia: reemplázala por lo que pida el reto ---
@tool
def consultar_inventario(producto: str) -> str:
    """Consulta el stock disponible de un producto.

    Args:
        producto: nombre del producto
    """
    inventario = {"laptop": 12, "mouse": 140, "monitor": 0}  # ← conecta tu API o BD real
    return f"{producto}: {inventario.get(producto.lower(), 'no existe')} unidades"


def reto_team(model: str | None = None, kb: KnowledgeBase | None = None) -> list[Agent]:
    docs = [kb.as_tool()] if kb and len(kb) else []
    base = f"Contexto del reto:\n{CONTEXTO_RETO}\nResponde en español y con formato Markdown."
    return [
        Agent(
            "asistente",
            f"Eres el asistente principal de la solución. {base}",
            tools=[consultar_inventario, read_file, *docs],
            description="Atiende las solicitudes principales del usuario.",
            model=model,
        ),
        Agent(
            "analista",
            f"Analizas datos con Python y das conclusiones con números. {base}",
            tools=[read_file, run_python, write_file, *docs],
            description="Hace análisis de datos y cálculos.",
            model=model,
        ),
        Agent(
            "investigador",
            f"Buscas información complementaria en internet y citas fuentes. {base}",
            tools=[web_search],
            description="Busca información externa en internet.",
            model=model,
        ),
    ]
