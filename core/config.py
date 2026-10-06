"""Configuración central: proveedores, modelos y orden de fallback.

Para agregar un proveedor nuevo basta con añadir una entrada a PROVIDERS.
Los nombres de modelo usan el formato de LiteLLM: "<proveedor>/<modelo>".
Si un modelo deja de existir, cámbialo aquí o en el .env (DEFAULT_MODEL / FALLBACK_MODELS).
"""

import os

from dotenv import load_dotenv

load_dotenv()

PROVIDERS: dict[str, dict] = {
    "gemini": {
        "env": "GEMINI_API_KEY",
        "models": [
            "gemini/gemini-2.5-flash",
            "gemini/gemini-2.5-pro",
            "gemini/gemini-2.5-flash-lite",
        ],
        "signup": "https://aistudio.google.com/apikey",
    },
    "groq": {
        "env": "GROQ_API_KEY",
        "models": [
            "groq/llama-3.3-70b-versatile",
            "groq/openai/gpt-oss-120b",
        ],
        "signup": "https://console.groq.com/keys",
    },
    "openrouter": {
        "env": "OPENROUTER_API_KEY",
        "models": [
            "openrouter/meta-llama/llama-3.3-70b-instruct:free",
            "openrouter/deepseek/deepseek-chat-v3-0324:free",
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
            "nvidia_nim/meta/llama-3.3-70b-instruct",
            "nvidia_nim/qwen/qwen2.5-coder-32b-instruct",
        ],
        "signup": "https://build.nvidia.com",
    },
    "cohere": {
        "env": "COHERE_API_KEY",
        "models": ["cohere/command-a-03-2025"],
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
