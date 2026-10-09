"""Tests de las variantes async de core/llm.py (issue #6). LiteLLM simulado: sin red ni keys reales."""

import asyncio
import time

import litellm
import pytest
from litellm import ModelResponse
from pydantic import BaseModel

from core import llm


class Emocion(BaseModel):
    emocion: str
    sentimiento: float


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    for var in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_NIM_API_KEY", "COHERE_API_KEY",
                "CEREBRAS_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "falsa")
    monkeypatch.setenv("COHERE_API_KEY", "falsa")
    monkeypatch.setenv("DEFAULT_MODEL", "groq/openai/gpt-oss-120b")
    monkeypatch.setenv("FALLBACK_MODELS", "groq/openai/gpt-oss-120b,cohere/command-a-03-2025")
    llm._cooldown.clear()
    yield
    llm._cooldown.clear()


def respuesta(texto: str) -> ModelResponse:
    return ModelResponse(choices=[{"message": {"role": "assistant", "content": texto}}])


def simular(monkeypatch, comportamiento):
    """comportamiento(model, kwargs) -> texto o excepción. Devuelve la lista de llamadas."""
    llamadas = []

    async def falso(model, messages, **kwargs):
        llamadas.append({"model": model, "messages": messages, **kwargs})
        await asyncio.sleep(0)
        resultado = comportamiento(model, kwargs)
        if isinstance(resultado, Exception):
            raise resultado
        return respuesta(resultado)

    monkeypatch.setattr(litellm, "acompletion", falso)
    return llamadas


def test_achat_responde_con_el_modelo_principal(monkeypatch):
    llamadas = simular(monkeypatch, lambda m, kw: "hola")
    r = asyncio.run(llm.achat([{"role": "user", "content": "hi"}]))
    assert r.choices[0].message.content == "hola"
    assert [c["model"] for c in llamadas] == ["groq/openai/gpt-oss-120b"]
    assert llamadas[0]["num_retries"] == 0 and llamadas[0]["temperature"] == 0.3


def test_achat_pasa_al_siguiente_modelo_si_falla(monkeypatch):
    def comp(m, kw):
        return RuntimeError("caído") if m.startswith("groq") else "desde cohere"
    llamadas = simular(monkeypatch, comp)
    r = asyncio.run(llm.achat([{"role": "user", "content": "hi"}]))
    assert r.choices[0].message.content == "desde cohere"
    assert [c["model"] for c in llamadas] == ["groq/openai/gpt-oss-120b", "cohere/command-a-03-2025"]


def test_achat_rate_limit_pone_el_modelo_en_enfriamiento(monkeypatch):
    def comp(m, kw):
        if m.startswith("groq"):
            return litellm.RateLimitError("cuota. Please retry in 30.5s", llm_provider="groq", model=m)
        return "ok"
    simular(monkeypatch, comp)
    asyncio.run(llm.achat([{"role": "user", "content": "hi"}]))
    assert 25 < llm._cooldown["groq/openai/gpt-oss-120b"] - time.time() <= 30.5
    # La siguiente llamada empieza por el modelo que no está en enfriamiento.
    llamadas = simular(monkeypatch, lambda m, kw: "ok")
    asyncio.run(llm.achat([{"role": "user", "content": "hi"}]))
    assert llamadas[0]["model"] == "cohere/command-a-03-2025"


def test_achat_todos_fallan_lanza_runtimeerror_con_el_detalle(monkeypatch):
    simular(monkeypatch, lambda m, kw: RuntimeError(f"fallo {m}"))
    with pytest.raises(RuntimeError, match="Todos los modelos fallaron") as e:
        asyncio.run(llm.achat([{"role": "user", "content": "hi"}]))
    assert "groq/openai/gpt-oss-120b" in str(e.value) and "cohere/command-a-03-2025" in str(e.value)


def test_achat_respeta_el_modelo_pedido_y_pasa_kwargs(monkeypatch):
    llamadas = simular(monkeypatch, lambda m, kw: "ok")
    asyncio.run(llm.achat([{"role": "user", "content": "hi"}], model="cohere/command-a-03-2025",
                          temperature=0, timeout=5))
    assert llamadas[0]["model"] == "cohere/command-a-03-2025"
    assert llamadas[0]["temperature"] == 0 and llamadas[0]["timeout"] == 5


def test_achat_no_bloquea_el_event_loop(monkeypatch):
    async def lento(model, messages, **kwargs):
        await asyncio.sleep(0.2)
        return respuesta("ok")
    monkeypatch.setattr(litellm, "acompletion", lento)

    async def dos_a_la_vez():
        inicio = time.perf_counter()
        await asyncio.gather(llm.achat([{"role": "user", "content": "a"}]),
                             llm.achat([{"role": "user", "content": "b"}]))
        return time.perf_counter() - inicio
    assert asyncio.run(dos_a_la_vez()) < 0.35  # en paralelo, no 0.4 s en serie


def test_astructured_devuelve_el_modelo_validado(monkeypatch):
    llamadas = simular(monkeypatch, lambda m, kw: '```json\n{"emocion": "ansiedad", "sentimiento": -0.4}\n```')
    e = asyncio.run(llm.astructured("Estoy preocupada", Emocion, system="Analiza la emoción."))
    assert e == Emocion(emocion="ansiedad", sentimiento=-0.4)
    sistema = llamadas[0]["messages"][0]
    assert sistema["role"] == "system" and "Analiza la emoción." in sistema["content"] and "JSON Schema" in sistema["content"]
    assert llamadas[0]["temperature"] == 0


def test_astructured_reintenta_si_el_json_es_invalido(monkeypatch):
    respuestas = iter(["no es json", '{"emocion": "calma", "sentimiento": 0.2}'])
    llamadas = simular(monkeypatch, lambda m, kw: next(respuestas))
    assert asyncio.run(llm.astructured("hola", Emocion)).emocion == "calma"
    assert len(llamadas) == 2
    assert "no es válido" in llamadas[1]["messages"][-1]["content"]


def test_astructured_falla_tras_agotar_reintentos(monkeypatch):
    simular(monkeypatch, lambda m, kw: "nunca json")
    with pytest.raises(ValueError, match="Emocion"):
        asyncio.run(llm.astructured("hola", Emocion, retries=1))


def test_structured_sincrono_sigue_funcionando(monkeypatch):
    monkeypatch.setattr(litellm, "completion",
                        lambda model, messages, **kw: respuesta('{"emocion": "alegria", "sentimiento": 0.9}'))
    assert llm.structured("genial", Emocion).emocion == "alegria"
