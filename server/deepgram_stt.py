"""STT con diarización para el panel de transcripción (spec §5.3 T3, 15 % de la nota).

Corre EN PARALELO al Voice Agent, con el mismo audio del micrófono: el agente decide los turnos y
este STT solo dibuja quién dijo qué (Hablante 1, Hablante 2...) con marcas de tiempo.

Verificado contra la API real (2026-10-09): `diarize_model=latest` (diarize=true está obsoleto) separa dos
voces en un solo canal; cada palabra trae `speaker`, `start`, `end` y `punctuated_word`. Los resultados
llegan como parciales (`is_final=false`) que se reemplazan hasta el final del fragmento (`is_final=true`).
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from urllib.parse import urlencode

import websockets

log = logging.getLogger("cognia.stt")

BASE = "wss://api.deepgram.com/v1/listen"
TIMEOUT_APERTURA_S = 10.0

# Palabras que el STT debe favorecer (Nova-3 keyterm): sin esto "Cali" sale como "calle".
KEYTERMS = ["IPS", "EPS", "UCI", "Cali", "Medellín", "Bogotá", "Barranquilla", "Cartagena", "Bucaramanga",
            "Pereira", "Manizales", "Armenia", "Palmira", "Cúcuta", "Pasto", "Popayán", "Neiva", "Ibagué"]


def url(keyterms: list[str] | None = None) -> str:
    params = [("model", "nova-3"), ("language", "es"), ("diarize_model", "latest"), ("interim_results", "true"),
              ("smart_format", "true"), ("encoding", "linear16"), ("sample_rate", "16000"), ("channels", "1")]
    params += [("keyterm", k) for k in (KEYTERMS if keyterms is None else keyterms)]
    return f"{BASE}?{urlencode(params)}"


@dataclass(frozen=True)
class Segmento:
    hablante: int  # 0, 1, ... (Deepgram); en la UI "Hablante N+1"
    texto: str
    inicio: float
    fin: float


def segmentos(resultado: dict) -> list[Segmento]:
    """Parte un mensaje Results en tramos consecutivos del mismo hablante."""
    try:
        palabras = resultado["channel"]["alternatives"][0].get("words") or []
    except (KeyError, IndexError, TypeError):
        return []
    tramos: list[Segmento] = []
    for p in palabras:
        hablante = int(p.get("speaker") or 0)
        texto = p.get("punctuated_word") or p.get("word") or ""
        inicio, fin = float(p.get("start") or 0), float(p.get("end") or 0)
        if tramos and tramos[-1].hablante == hablante:
            ultimo = tramos[-1]
            tramos[-1] = Segmento(hablante, f"{ultimo.texto} {texto}", ultimo.inicio, fin)
        else:
            tramos.append(Segmento(hablante, texto, inicio, fin))
    return [t for t in tramos if t.texto.strip()]


class ErrorSTT(RuntimeError):
    pass


class ConexionSTT:
    def __init__(self, ws):
        self._ws = ws

    @classmethod
    async def abrir(cls, api_key: str, direccion: str | None = None) -> "ConexionSTT":
        if not api_key:
            raise ErrorSTT("falta DEEPGRAM_API_KEY")
        try:
            ws = await asyncio.wait_for(websockets.connect(
                direccion or url(), additional_headers={"Authorization": f"Token {api_key}"}, max_size=None,
                ping_interval=20), TIMEOUT_APERTURA_S)
        except Exception as e:  # noqa: BLE001
            raise ErrorSTT(f"no pude conectar el STT ({type(e).__name__}: {e})"[:300]) from e
        return cls(ws)

    async def enviar_audio(self, pcm: bytes) -> None:
        await self._ws.send(pcm)

    async def keepalive(self) -> None:
        await self._ws.send(json.dumps({"type": "KeepAlive"}))

    def __aiter__(self):
        return self._recibir()

    async def _recibir(self):
        """Solo los mensajes Results (dict)."""
        try:
            async for m in self._ws:
                if isinstance(m, str):
                    d = json.loads(m)
                    if d.get("type") == "Results":
                        yield d
        except websockets.ConnectionClosed:
            return

    async def cerrar(self) -> None:
        try:
            await self._ws.send(json.dumps({"type": "CloseStream"}))
            await self._ws.close()
        except Exception:  # noqa: BLE001
            pass
