"""Configuración central: proveedores, modelos y orden de fallback.

Para agregar un proveedor nuevo basta con añadir una entrada a PROVIDERS.
Los nombres de modelo usan el formato de LiteLLM: "<proveedor>/<modelo>".
Si un modelo deja de existir, corre `uv run python list_models.py` para ver los vigentes
y cámbialo aquí o en el .env (DEFAULT_MODEL / FALLBACK_MODELS).
"""

import os

from dotenv import load_dotenv

load_dotenv()

PROVIDERS: dict[str, dict] = {
    "gemini": {
        "env": "GEMINI_API_KEY",
        "models": [
            "gemini/gemini-3.5-flash",
            "gemini/gemini-3.8-flash",
        ],
        "signup": "https://aistudio.google.com/apikey",
    },
    "groq": {
        "env": "GROQ_API_KEY",
        "models": [
            "groq/openai/gpt-oss-120b",
            "groq/qwen/qwen3.8-27b",
            "groq/openai/gpt-oss-20b",
        ],
        "signup": "https://console.groq.com/keys",
    },
    "openrouter": {
        "env": "OPENROUTER_API_KEY",
        "models": [
            "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
            "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        ],
        "signup": "https://openrouter.ai/keys",
    },
    "cerebras": {
        "env": "CEREBRAS_API_KEY",
        "models": ["cerebras/llama-3.3-70b"],
        "signup": "https://cloud.cerebras.ai",
    },
    "nvidia_nim": {
        "env": "NVIDIA_NIM_API_KEY",
        "models": [
            "nvidia_nim/nvidia/nemotron-3-super-120b-a12b",
            "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b",
        ],
        "signup": "https://build.nvidia.com",
    },
    "cohere": {
        "env": "COHERE_API_KEY",
        "models": ["cohere/command-a-plus-05-2026", "cohere/command-a-03-2025"],
        "signup": "https://dashboard.cohere.com/api-keys",
    },
    "mistral": {
        "env": "MISTRAL_API_KEY",
        "models": ["mistral/mistral-small-latest"],
        "signup": "https://console.mistral.ai/api-keys",
    },
}

EMBED_MODEL = os.getenv("EMBED_MODEL", "gemini/gemini-embedding-001")


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def has_key(provider: str) -> bool:
    return bool(os.getenv(PROVIDERS[provider]["env"]))


def provider_of(model: str) -> str:
    return model.split("/", 1)[0]


def available_models() -> list[str]:
    """Modelos de los proveedores que tienen API key configurada."""
    return [m for p, cfg in PROVIDERS.items() if has_key(p) for m in cfg["models"]]


def default_model() -> str:
    env = os.getenv("DEFAULT_MODEL")
    if env:
        return env
    models = available_models()
    if not models:
        raise RuntimeError("No hay API keys configuradas. Copia .env.example a .env y llénalo.")
    return models[0]


def fallback_chain() -> list[str]:
    """Orden de modelos a probar si el principal falla (rate limit, caída, etc.)."""
    env = _split(os.getenv("FALLBACK_MODELS"))
    if env:
        return env
    # Por defecto: el primer modelo de cada proveedor disponible.
    return [cfg["models"][0] for p, cfg in PROVIDERS.items() if has_key(p)]
