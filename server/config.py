"""Configuración del servidor del reto, leída de variables de entorno (.env en local, Railway en producción)."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Sin esto los logs "cognia.*" no salen (uvicorn solo configura los suyos) y en Railway no se ve nada.
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

RAIZ = Path(__file__).resolve().parent.parent
WEB_DIR = RAIZ / "web"

VOZ = os.getenv("VOICE_MODEL", "aura-2-celeste-es")
