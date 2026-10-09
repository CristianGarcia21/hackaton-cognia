"""Capa LLM única. Todo el proyecto habla con los modelos a través de este módulo.

- chat():       completado con fallback automático entre proveedores.
- stream():     igual pero devuelve trozos de texto (para la UI).
- structured(): devuelve un objeto Pydantic validado (extraer / clasificar datos).
- embed():      embeddings para RAG.
- achat() / astructured(): versiones async (backend FastAPI): no bloquean el event loop y comparten
  el mismo fallback y enfriamiento por rate limit que las síncronas.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Iterator

import litellm
import numpy as np
from pydantic import BaseModel

from core import config

litellm.drop_params = True  # ignora parámetros que un proveedor no soporte
litellm.suppress_debug_info = True
log = logging.getLogger("cognia.llm")


# Modelo -> momento hasta el que se salta por haber agotado su cuota (rate limit).
_cooldown: dict[str, float] = {}


def _retry_seconds(message: str) -> float | None:
    """Segundos de espera que pide el proveedor en un rate limit, o None si no lo dice.
    Gemini: "retry in 40.6s" / "retryDelay": "40s". Groq: "try again in 1.234s", "2m3.5s", "450ms"."""
    m = re.search(r"(?:retry|try again) in (?:(\d+)m)?([\d.]+)(ms|s)\b", message)
    if m:
        return int(m[1] or 0) * 60 + float(m[2]) / (1000 if m[3] == "ms" else 1)
    m = re.search(r'"retryDelay":\s*"([\d.]+)s"', message)
    return float(m[1]) if m else None


def _on_failure(model: str, error: Exception) -> str:
    """Registra el fallo y, si es por cuota, pone el modelo en enfriamiento solo lo que pide el proveedor."""
    if isinstance(error, litellm.RateLimitError):
        wait = _retry_seconds(str(error))
        _cooldown[model] = time.time() + (wait if wait is not None else 60)
    summary = f"{model}: {type(error).__name__}: {str(error).splitlines()[0][:150]}"
    log.warning("Falló %s", summary)
    return summary


def _candidates(model: str | None) -> list[str]:
    chain = [model or config.default_model(), *config.fallback_chain()]
    seen, ready, cooling = set(), [], []
    for m in chain:
        # Cualquier modelo vale (aunque no esté en PROVIDERS) si su proveedor tiene key.
        provider = config.provider_of(m)
        if m not in seen and provider in config.PROVIDERS and config.has_key(provider):
            seen.add(m)
            (cooling if _cooldown.get(m, 0) > time.time() else ready).append(m)
    return ready + cooling  # los que están en enfriamiento solo como último recurso


def _request(m: str, messages: list[dict], tools: list[dict] | None, temperature: float, kwargs: dict) -> dict:
    # Gemini 3+ deprecó temperature (recomienda guiar el muestreo desde el prompt).
    sampling = {} if config.provider_of(m) == "gemini" else {"temperature": temperature}
    return {"model": m, "messages": messages, "tools": tools or None, "num_retries": 0, **sampling, **kwargs}


def _all_failed(errors: list[str]) -> RuntimeError:
    if not errors:
        return RuntimeError("Ningún modelo candidato tiene API key: revisa DEFAULT_MODEL, FALLBACK_MODELS y el .env")
    return RuntimeError("Todos los modelos fallaron:\n" + "\n".join(errors))


def chat(messages: list[dict], model: str | None = None, tools: list[dict] | None = None,
         temperature: float = 0.3, **kwargs):
    """Llama al modelo; si falla, prueba con el siguiente de la cadena de fallback.

    Devuelve la respuesta cruda de LiteLLM (formato OpenAI). El modelo que respondió
    queda en `response.model`.
    """
    errors = []
    for m in _candidates(model):
        try:
            return litellm.completion(**_request(m, messages, tools, temperature, kwargs))
        except Exception as e:  # noqa: BLE001 — cualquier fallo pasa al siguiente modelo
            errors.append(_on_failure(m, e))
    raise _all_failed(errors)


async def achat(messages: list[dict], model: str | None = None, tools: list[dict] | None = None,
                temperature: float = 0.3, total_timeout: float | None = None, **kwargs):
    """Versión async de chat(): mismo fallback y enfriamiento, sin bloquear el event loop.

    timeout=<s> limita cada proveedor; total_timeout=<s> limita la cadena completa de fallback y lanza
    TimeoutError (en voz usa ambos: p. ej. timeout=2, total_timeout=4). Cancelar la tarea la detiene
    sin probar más modelos."""
    async with asyncio.timeout(total_timeout):
        errors = []
        for m in _candidates(model):
            try:
                return await litellm.acompletion(**_request(m, messages, tools, temperature, kwargs))
            except Exception as e:  # noqa: BLE001 — cualquier fallo pasa al siguiente modelo
                errors.append(_on_failure(m, e))
        raise _all_failed(errors)


def ask(prompt: str, system: str | None = None, model: str | None = None, **kwargs) -> str:
    """Atajo: un prompt de texto, una respuesta de texto."""
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    return chat(messages, model=model, **kwargs).choices[0].message.content or ""


def stream(messages: list[dict], model: str | None = None, **kwargs) -> Iterator[str]:
    errors = []
    for m in _candidates(model):
        try:
            response = litellm.completion(model=m, messages=messages, stream=True, num_retries=0, **kwargs)
            for chunk in response:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
            return
        except Exception as e:  # noqa: BLE001
            errors.append(_on_failure(m, e))
    raise RuntimeError("Todos los modelos fallaron:\n" + "\n".join(errors))


def _extract_json(text: str) -> str:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=0)
    return text[start:].strip()


def _structured_messages(prompt: str | list[dict], schema: type[BaseModel], system: str | None) -> list[dict]:
    instructions = (
        "Responde ÚNICAMENTE con un objeto JSON válido que cumpla este JSON Schema, sin texto extra:\n"
        + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    )
    messages = prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
    return [{"role": "system", "content": f"{system or ''}\n\n{instructions}".strip()}, *messages]


def _correction(text: str, error: Exception) -> list[dict]:
    return [{"role": "assistant", "content": text},
            {"role": "user", "content": f"El JSON no es válido: {error}. Corrígelo y responde solo el JSON."}]


def structured[T: BaseModel](prompt: str | list[dict], schema: type[T], model: str | None = None,
                             system: str | None = None, retries: int = 2, **kwargs) -> T:
    """Devuelve una instancia de `schema` validada. Reintenta si el JSON no es válido.

    Funciona igual en todos los proveedores porque pide el JSON por prompt
    en lugar de depender del modo nativo de cada uno.
    """
    messages = _structured_messages(prompt, schema, system)
    kwargs.setdefault("temperature", 0)
    last_error = None
    for _ in range(retries + 1):
        text = chat(messages, model=model, **kwargs).choices[0].message.content or ""
        try:
            return schema.model_validate_json(_extract_json(text))
        except Exception as e:  # noqa: BLE001
            last_error = e
            messages += _correction(text, e)
    raise ValueError(f"No se obtuvo JSON válido para {schema.__name__}: {last_error}")


async def astructured[T: BaseModel](prompt: str | list[dict], schema: type[T], model: str | None = None,
                                    system: str | None = None, retries: int = 2,
                                    total_timeout: float | None = None, **kwargs) -> T:
    """Versión async de structured() (emociones, verificador, brief en el backend).
    total_timeout limita TODO (reintentos + fallback) y lanza TimeoutError."""
    messages = _structured_messages(prompt, schema, system)
    kwargs.setdefault("temperature", 0)
    async with asyncio.timeout(total_timeout):
        last_error = None
        for _ in range(retries + 1):
            text = (await achat(messages, model=model, **kwargs)).choices[0].message.content or ""
            try:
                return schema.model_validate_json(_extract_json(text))
            except Exception as e:  # noqa: BLE001
                last_error = e
                messages += _correction(text, e)
        raise ValueError(f"No se obtuvo JSON válido para {schema.__name__}: {last_error}")


def embed(texts: list[str], model: str | None = None) -> np.ndarray:
    """Embeddings normalizados (norma 1) para búsqueda por similitud coseno."""
    vectors = []
    for i in range(0, len(texts), 64):  # lotes para no pasar los límites del proveedor
        response = litellm.embedding(model=model or config.EMBED_MODEL, input=texts[i:i + 64])
        vectors += [d["embedding"] for d in response.data]
    arr = np.array(vectors, dtype=np.float32)
    return arr / np.clip(np.linalg.norm(arr, axis=1, keepdims=True), 1e-9, None)
