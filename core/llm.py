"""Capa LLM única. Todo el proyecto habla con los modelos a través de este módulo.

- chat():       completado con fallback automático entre proveedores.
- stream():     igual pero devuelve trozos de texto (para la UI).
- structured(): devuelve un objeto Pydantic validado (extraer / clasificar datos).
- embed():      embeddings para RAG.
"""

import json
import logging
import re
from collections.abc import Iterator

import litellm
import numpy as np
from pydantic import BaseModel

from core import config

litellm.drop_params = True  # ignora parámetros que un proveedor no soporte
litellm.suppress_debug_info = True
log = logging.getLogger("cognia.llm")


def _candidates(model: str | None) -> list[str]:
    chain = [model or config.default_model(), *config.fallback_chain()]
    seen, out = set(), []
    for m in chain:
        # Cualquier modelo vale (aunque no esté en PROVIDERS) si su proveedor tiene key.
        provider = config.provider_of(m)
        if m not in seen and provider in config.PROVIDERS and config.has_key(provider):
            seen.add(m)
            out.append(m)
    return out


def chat(messages: list[dict], model: str | None = None, tools: list[dict] | None = None,
         temperature: float = 0.3, **kwargs):
    """Llama al modelo; si falla, prueba con el siguiente de la cadena de fallback.

    Devuelve la respuesta cruda de LiteLLM (formato OpenAI). El modelo que respondió
    queda en `response.model`.
    """
    errors = []
    for m in _candidates(model):
        try:
            return litellm.completion(
                model=m, messages=messages, tools=tools or None,
                temperature=temperature, num_retries=1, **kwargs,
            )
        except Exception as e:  # noqa: BLE001 — cualquier fallo pasa al siguiente modelo
            log.warning("Modelo %s falló: %s", m, e)
            errors.append(f"{m}: {type(e).__name__}: {str(e)[:200]}")
    raise RuntimeError("Todos los modelos fallaron:\n" + "\n".join(errors))


def ask(prompt: str, system: str | None = None, model: str | None = None, **kwargs) -> str:
    """Atajo: un prompt de texto, una respuesta de texto."""
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    return chat(messages, model=model, **kwargs).choices[0].message.content or ""


def stream(messages: list[dict], model: str | None = None, **kwargs) -> Iterator[str]:
    errors = []
    for m in _candidates(model):
        try:
            response = litellm.completion(model=m, messages=messages, stream=True, num_retries=1, **kwargs)
            for chunk in response:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
            return
        except Exception as e:  # noqa: BLE001
            log.warning("Streaming con %s falló: %s", m, e)
            errors.append(f"{m}: {e}")
    raise RuntimeError("Todos los modelos fallaron:\n" + "\n".join(errors))


def _extract_json(text: str) -> str:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=0)
    return text[start:].strip()


def structured[T: BaseModel](prompt: str | list[dict], schema: type[T], model: str | None = None,
                             system: str | None = None, retries: int = 2) -> T:
    """Devuelve una instancia de `schema` validada. Reintenta si el JSON no es válido.

    Funciona igual en todos los proveedores porque pide el JSON por prompt
    en lugar de depender del modo nativo de cada uno.
    """
    instructions = (
        "Responde ÚNICAMENTE con un objeto JSON válido que cumpla este JSON Schema, sin texto extra:\n"
        + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    )
    messages = prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
    messages = [{"role": "system", "content": f"{system or ''}\n\n{instructions}".strip()}, *messages]

    last_error = None
    for _ in range(retries + 1):
        text = chat(messages, model=model, temperature=0).choices[0].message.content or ""
        try:
            return schema.model_validate_json(_extract_json(text))
        except Exception as e:  # noqa: BLE001
            last_error = e
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"El JSON no es válido: {e}. Corrígelo y responde solo el JSON."},
            ]
    raise ValueError(f"No se obtuvo JSON válido para {schema.__name__}: {last_error}")


def embed(texts: list[str], model: str | None = None) -> np.ndarray:
    """Embeddings normalizados (norma 1) para búsqueda por similitud coseno."""
    vectors = []
    for i in range(0, len(texts), 64):  # lotes para no pasar los límites del proveedor
        response = litellm.embedding(model=model or config.EMBED_MODEL, input=texts[i:i + 64])
        vectors += [d["embedding"] for d in response.data]
    arr = np.array(vectors, dtype=np.float32)
    return arr / np.clip(np.linalg.norm(arr, axis=1, keepdims=True), 1e-9, None)
