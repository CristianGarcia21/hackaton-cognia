"""Lista los modelos de chat disponibles HOY en cada proveedor con key:  uv run python list_models.py

Útil cuando test_keys.py marca un modelo como inexistente: copia uno de aquí a core/config.py.
Filtro opcional:  uv run python list_models.py llama
"""

import os
import sys

import httpx

from core import config

sys.stdout.reconfigure(encoding="utf-8")


def _get(url: str, headers: dict | None = None) -> dict:
    response = httpx.get(url, headers=headers or {}, timeout=30)
    response.raise_for_status()
    return response.json()


def gemini(key: str) -> list[str]:
    data = _get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000", {"x-goog-api-key": key})
    return [m["name"].removeprefix("models/") for m in data["models"]
            if "generateContent" in m.get("supportedGenerationMethods", [])]


def openai_like(base: str):
    def fetch(key: str) -> list[str]:
        return [m["id"] for m in _get(f"{base}/models", {"Authorization": f"Bearer {key}"})["data"]]
    return fetch


def openrouter(key: str) -> list[str]:
    # Solo modelos gratis que soportan tool calling (necesario para los agentes).
    data = _get("https://openrouter.ai/api/v1/models", {"Authorization": f"Bearer {key}"})["data"]
    return [m["id"] for m in data if m["id"].endswith(":free") and "tools" in m.get("supported_parameters", [])]


def cohere(key: str) -> list[str]:
    data = _get("https://api.cohere.com/v1/models?endpoint=chat&page_size=1000", {"Authorization": f"Bearer {key}"})
    return [m["name"] for m in data["models"]]


FETCHERS = {
    "gemini": gemini,
    "groq": openai_like("https://api.groq.com/openai/v1"),
    "openrouter": openrouter,
    "nvidia_nim": openai_like("https://integrate.api.nvidia.com/v1"),
    "cohere": cohere,
    "cerebras": openai_like("https://api.cerebras.ai/v1"),
    "mistral": openai_like("https://api.mistral.ai/v1"),
}


def main() -> None:
    term = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    for provider, fetch in FETCHERS.items():
        if not config.has_key(provider):
            continue
        try:
            models = sorted(m for m in fetch(os.environ[config.PROVIDERS[provider]["env"]]) if term in m.lower())
        except Exception as e:  # noqa: BLE001
            print(f"\n❌ {provider}: {e}")
            continue
        print(f"\n🔑 {provider} ({len(models)})")
        for m in models:
            print(f"   {provider}/{m}")


if __name__ == "__main__":
    main()
