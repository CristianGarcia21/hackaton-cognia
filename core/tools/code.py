"""Ejecución de código Python para cálculos y análisis de datos.

OJO: corre en un subproceso local con timeout, NO es un sandbox real.
Está bien para una hackatón; no lo expongas a usuarios desconocidos.
"""

import subprocess
import sys
from pathlib import Path

from core.tools import tool


@tool
def run_python(code: str) -> str:
    """Ejecuta código Python y devuelve lo que imprime. Tiene pandas y numpy. Usa print() para ver resultados.

    Args:
        code: código Python a ejecutar
    """
    Path("outputs").mkdir(exist_ok=True)
    try:
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return "Error: el código tardó más de 60 segundos."
    output = (result.stdout + ("\n[stderr]\n" + result.stderr if result.stderr else "")).strip()
    return output[:10_000] or "(sin salida, ¿olvidaste print()?)"
