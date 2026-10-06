"""Búsqueda web gratuita (DuckDuckGo, sin API key) y lectura de páginas."""

import httpx
from bs4 import BeautifulSoup

from core.tools import tool


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Busca en internet y devuelve títulos, URLs y fragmentos de los resultados.

    Args:
        query: lo que se quiere buscar
        max_results: número de resultados (1-10)
    """
    from ddgs import DDGS

    try:
        results = DDGS().text(query, max_results=min(max_results, 10))
    except Exception as e:  # noqa: BLE001
        return f"Error en la búsqueda: {e}"
    if not results:
        return "Sin resultados."
    return "\n\n".join(f"- {r['title']}\n  {r['href']}\n  {r['body']}" for r in results)


@tool
def fetch_url(url: str) -> str:
    """Descarga una página web y devuelve su texto legible (sin HTML).

    Args:
        url: dirección completa de la página
    """
    try:
        response = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return f"Error al descargar {url}: {e}"
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    return text[:15_000]
