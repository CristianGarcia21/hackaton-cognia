"""Lanzador de la UI (Windows, Linux, macOS y WSL):  uv run python run.py

Por qué no usar `streamlit run app.py` directamente:
LiteLLM instala un filtro de logging que importa módulos de LiteLLM de forma perezosa.
Si el servidor de Streamlit registra un log mientras el hilo del script todavía está
importando LiteLLM, los dos hilos se bloquean (_DeadlockError). Pasa sobre todo en WSL,
donde importar desde /mnt/... es lento. Importando todo aquí, en el hilo principal y
antes de arrancar el servidor, los módulos ya están cargados cuando corre app.py.

Acepta los mismos argumentos que `streamlit run`, ej:  uv run python run.py --server.port 8502
"""

import sys

import litellm  # noqa: F401
import litellm.rust_bridge.catalog  # noqa: F401 — lo que importa el filtro de logs de LiteLLM
import litellm.rust_bridge.diagnostics  # noqa: F401
from streamlit.web import cli

import agents  # noqa: F401 — precarga core y agentes (y con ellos sus dependencias)

if __name__ == "__main__":
    sys.argv = ["streamlit", "run", "app.py", *sys.argv[1:]]
    sys.exit(cli.main())
