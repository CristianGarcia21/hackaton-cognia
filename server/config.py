"""Configuración del servidor del reto, leída de variables de entorno (.env en local, Railway en producción)."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Sin esto los logs "cognia.*" no salen (uvicorn solo configura los suyos) y en Railway no se ve nada.
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # una línea por consulta a la API ensucia los logs

RAIZ = Path(__file__).resolve().parent.parent
WEB_DIR = RAIZ / "web"

VOZ = os.getenv("VOICE_MODEL", "aura-2-celeste-es")

# API key de datos.gov.co (Portal del desarrollador): SODA3 la acepta como auth Basic (id, secreto).
_kid, _ksecret = os.getenv("DATOS_GOV_KEY_ID"), os.getenv("DATOS_GOV_KEY_SECRET")
DATOS_GOV_AUTH: tuple[str, str] | None = (_kid, _ksecret) if _kid and _ksecret else None

# Voz (issue #10): Deepgram Voice Agent con Groq como LLM ("think"). Sin DEEPGRAM_API_KEY la página carga
# pero la voz responde con un error claro.
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
VOICE_LLM = os.getenv("VOICE_LLM", "openai/gpt-oss-20b")  # el único de Groq que Deepgram lista; ≈0,9 s por turno
