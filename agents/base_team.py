"""Equipo genérico que sirve para casi cualquier reto mientras armas el tuyo."""

from core.agent import Agent
from core.memory import KnowledgeBase
from core.tools.code import run_python
from core.tools.files import describe_table, read_file, write_file
from core.tools.web import fetch_url, web_search

RULES = "Responde siempre en español, de forma clara y con formato Markdown. Si no sabes algo, dilo."


def base_team(model: str | None = None, kb: KnowledgeBase | None = None) -> list[Agent]:
    docs = [kb.as_tool()] if kb and len(kb) else []
    return [
        Agent(
            "investigador",
            f"Investigas temas en internet. Busca, abre las fuentes más relevantes y cita las URLs. {RULES}",
            tools=[web_search, fetch_url],
            description="Busca información actual en internet y cita fuentes.",
            model=model,
        ),
        Agent(
            "analista",
            "Eres analista de datos. Para cualquier cálculo usa run_python (con pandas) en lugar de calcular "
            f"de cabeza. Lee los archivos que te indiquen y explica los hallazgos con números. {RULES}",
            tools=[read_file, describe_table, run_python, write_file, *docs],
            description="Analiza archivos CSV/Excel/PDF, hace cálculos y estadísticas con Python.",
            model=model,
        ),
        Agent(
            "documentos",
            "Respondes preguntas sobre los documentos del usuario. Usa search_documents o read_file y basa tu "
            f"respuesta solo en lo que encuentres, citando el archivo de origen. {RULES}",
            tools=[read_file, *docs],
            description="Responde preguntas sobre los documentos que subió el usuario (RAG).",
            model=model,
        ),
        Agent(
            "redactor",
            f"Redactas textos claros y bien estructurados: reportes, resúmenes, correos, propuestas. {RULES}",
            tools=[write_file],
            description="Redacta y da formato a reportes, resúmenes, correos o propuestas.",
            model=model,
        ),
    ]
