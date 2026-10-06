"""Verifica que cada API key funcione ANTES de la hackatón:  uv run python test_keys.py

Prueba texto, tool-calling y embeddings para cada proveedor configurado.
"""

import sys
import time

import litellm

from core import config

litellm.suppress_debug_info = True
sys.stdout.reconfigure(encoding="utf-8")  # la consola de Windows no imprime emojis sin esto

TOOL = {
    "type": "function",
    "function": {
        "name": "sumar",
        "description": "Suma dos números",
        "parameters": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                       "required": ["a", "b"]},
    },
}


def check(label: str, fn) -> None:
    start = time.time()
    try:
        detail = fn()
        print(f"  ✅ {label:<10} {time.time() - start:5.1f}s  {detail}")
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ {label:<10} {type(e).__name__}: {str(e)[:160]}")


def main() -> None:
    for provider, cfg in config.PROVIDERS.items():
        if not config.has_key(provider):
            print(f"\n⚪ {provider}: sin key ({cfg['env']}) → {cfg['signup']}")
            continue
        print(f"\n🔑 {provider}")
        for model in cfg["models"]:
            print(f" {model}")
            check("texto", lambda m=model: litellm.completion(
                # max_tokens holgado: los modelos con "thinking" gastan tokens razonando antes de responder.
                model=m, messages=[{"role": "user", "content": "Responde solo: OK"}], max_tokens=1000,
            ).choices[0].message.content.strip()[:30])
            check("tools", lambda m=model: litellm.completion(
                model=m, messages=[{"role": "user", "content": "¿Cuánto es 2+3? Usa la herramienta."}], tools=[TOOL],
            ).choices[0].message.tool_calls[0].function.arguments)

    if config.has_key(config.provider_of(config.EMBED_MODEL)):
        print(f"\n🧩 embeddings ({config.EMBED_MODEL})")
        check("embed", lambda: f"dim={len(litellm.embedding(model=config.EMBED_MODEL, input=['hola']).data[0]['embedding'])}")

    print(f"\nModelo por defecto: {config.default_model() if config.available_models() else '—'}")
    print(f"Cadena de fallback: {config.fallback_chain()}")


if __name__ == "__main__":
    main()
