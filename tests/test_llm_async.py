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
    en_vuelo, maximo = 0, 0

    async def lento(model, messages, **kwargs):
        nonlocal en_vuelo, maximo
        en_vuelo += 1
        maximo = max(maximo, en_vuelo)
        await asyncio.sleep(0.05)
        en_vuelo -= 1
        return respuesta("ok")
    monkeypatch.setattr(litellm, "acompletion", lento)

    async def dos_a_la_vez():
        await asyncio.gather(llm.achat([{"role": "user", "content": "a"}]),
                             llm.achat([{"role": "user", "content": "b"}]))
    asyncio.run(dos_a_la_vez())
    assert maximo == 2  # las dos llamadas estuvieron en vuelo a la vez


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


# ---------------------- Revisión QA de #6 ----------------------

@pytest.mark.parametrize("mensaje,segundos", [
    ("Please retry in 40.6s.", 40.6),                                    # Gemini
    ("Rate limit reached... Please try again in 1.234s. Visit", 1.234),  # Groq
    ("Please try again in 2m3.5s.", 123.5),                              # Groq (minutos)
    ("Please try again in 450ms.", 0.45),                                # Groq (ms)
    ('"retryDelay": "17s"', 17.0),                                       # Gemini JSON
    ("cuota agotada sin pista", None),
])
def test_segundos_de_espera_del_rate_limit(mensaje, segundos):
    resultado = llm._retry_seconds(mensaje)
    assert resultado == pytest.approx(segundos) if segundos is not None else resultado is None


def test_rate_limit_de_groq_enfria_solo_lo_que_pide(monkeypatch):
    def comp(m, kw):
        if m.startswith("groq"):
            return litellm.RateLimitError("Please try again in 1.5s.", llm_provider="groq", model=m)
        return "ok"
    simular(monkeypatch, comp)
    asyncio.run(llm.achat([{"role": "user", "content": "hi"}]))
    assert 0 < llm._cooldown["groq/openai/gpt-oss-120b"] - time.time() <= 1.5


def test_total_timeout_corta_la_cadena_completa(monkeypatch):
    async def colgado(model, messages, **kwargs):
        await asyncio.sleep(5)
    monkeypatch.setattr(litellm, "acompletion", colgado)
    inicio = time.perf_counter()
    with pytest.raises(TimeoutError):
        asyncio.run(llm.achat([{"role": "user", "content": "hi"}], total_timeout=0.1))
    assert time.perf_counter() - inicio < 1


def test_astructured_respeta_total_timeout(monkeypatch):
    async def colgado(model, messages, **kwargs):
        await asyncio.sleep(5)
    monkeypatch.setattr(litellm, "acompletion", colgado)
    with pytest.raises(TimeoutError):
        asyncio.run(llm.astructured("hola", Emocion, total_timeout=0.1))


def test_timeout_de_un_proveedor_pasa_al_siguiente(monkeypatch):
    def comp(m, kw):
        return litellm.Timeout("lento", model=m, llm_provider="groq") if m.startswith("groq") else "cohere"
    simular(monkeypatch, comp)
    assert asyncio.run(llm.achat([{"role": "user", "content": "hi"}])).choices[0].message.content == "cohere"


def test_cancelar_la_tarea_no_pasa_al_siguiente_modelo(monkeypatch):
    llamadas = []

    async def lento(model, messages, **kwargs):
        llamadas.append(model)
        await asyncio.sleep(5)
    monkeypatch.setattr(litellm, "acompletion", lento)

    async def cancelar():
        tarea = asyncio.create_task(llm.achat([{"role": "user", "content": "hi"}]))
        await asyncio.sleep(0.05)
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea
    asyncio.run(cancelar())
    assert llamadas == ["groq/openai/gpt-oss-120b"]


def test_structured_acepta_temperature_y_kwargs(monkeypatch):
    llamadas = simular(monkeypatch, lambda m, kw: '{"emocion": "calma", "sentimiento": 0}')
    asyncio.run(llm.astructured("hola", Emocion, temperature=0.5, timeout=3))
    assert llamadas[0]["temperature"] == 0.5 and llamadas[0]["timeout"] == 3


def test_structured_no_muta_el_prompt_del_llamador(monkeypatch):
    respuestas = iter(["mal", '{"emocion": "calma", "sentimiento": 0}'])
    simular(monkeypatch, lambda m, kw: next(respuestas))
    prompt = [{"role": "user", "content": "hola"}]
    asyncio.run(llm.astructured(prompt, Emocion))
    assert prompt == [{"role": "user", "content": "hola"}]


def test_sin_candidatos_el_error_lo_explica(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY")
    monkeypatch.delenv("COHERE_API_KEY")
    with pytest.raises(RuntimeError, match="Ningún modelo candidato tiene API key"):
        asyncio.run(llm.achat([{"role": "user", "content": "hi"}]))
