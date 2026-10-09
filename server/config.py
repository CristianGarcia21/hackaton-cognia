"""Configuración del servidor del reto, leída de variables de entorno (.env en local, Railway en producción)."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

RAIZ = Path(__file__).resolve().parent.parent
WEB_DIR = RAIZ / "web"

VOZ = os.getenv("VOICE_MODEL", "aura-2-celeste-es")
