"""Lectura de archivos comunes: PDF, Word, Excel, CSV, JSON y texto plano."""

from pathlib import Path

import pandas as pd

from core.tools import tool

MAX_CHARS = 20_000


def extract_text(path: str | Path) -> str:
    """Convierte un archivo a texto. La usan tanto la tool como el RAG."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(path)
        return "\n\n".join(f"[Página {i + 1}]\n{page.extract_text() or ''}" for i, page in enumerate(reader.pages))
    if suffix == ".docx":
        from docx import Document

        return "\n".join(p.text for p in Document(path).paragraphs)
    if suffix in (".csv", ".xlsx", ".xls"):
        df = pd.read_csv(path) if suffix == ".csv" else pd.read_excel(path)
        return f"{len(df)} filas x {len(df.columns)} columnas\nColumnas: {list(df.columns)}\n\n{df.to_csv(index=False)}"
    return path.read_text(encoding="utf-8", errors="replace")


@tool
def read_file(path: str) -> str:
    """Lee un archivo (PDF, DOCX, CSV, XLSX, JSON, TXT, MD) y devuelve su contenido como texto.

    Args:
        path: ruta del archivo a leer
    """
    try:
        text = extract_text(path)
    except FileNotFoundError:
        return f"Error: no existe el archivo {path}"
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + f"\n\n[... truncado, {len(text) - MAX_CHARS} caracteres más. Usa run_python para analizarlo completo]"
    return text


@tool
def describe_table(path: str) -> str:
    """Resumen estadístico de un CSV o Excel: columnas, tipos, nulos y estadísticas básicas.

    Args:
        path: ruta del archivo CSV o XLSX
    """
    df = pd.read_csv(path) if path.lower().endswith(".csv") else pd.read_excel(path)
    return (
        f"Forma: {df.shape}\n\nTipos:\n{df.dtypes}\n\nNulos:\n{df.isna().sum()}\n\n"
        f"Estadísticas:\n{df.describe(include='all').to_string()}\n\nPrimeras filas:\n{df.head(10).to_string()}"
    )


@tool
def write_file(path: str, content: str) -> str:
    """Guarda texto en un archivo dentro de la carpeta outputs/ (reportes, código, JSON...).

    Args:
        path: nombre del archivo, ej. reporte.md
        content: contenido a escribir
    """
    target = Path("outputs") / Path(path).name
    target.parent.mkdir(exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Guardado en {target.resolve()}"
