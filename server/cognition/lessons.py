"""Memoria de lecciones (spec §10.3): el agente mejora entre sesiones SIN reentrenar, mejorando su contexto.

    lecciones = Lecciones()                                   # SQLite en data/lecciones.db
    await lecciones.agregar("keyterm", "Tuluá", "corrección del usuario")   # -> Lesson | None (None si ya estaba)
    lecciones.keyterms()        # -> listen.keyterms del agente y del STT
    lecciones.para_prompt()     # -> bloque que se agrega al prompt (reglas y sinónimos)

Fuentes: el verificador (⚠ no respaldado → regla), las correcciones del usuario ("no, dije Palmira" →
keyterm) y el agente de pruebas QA (usa `agregar` desde qa/). No es reentrenamiento: es contexto.
"""

import asyncio
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from server import config
from server import events as ev
from server.seguridad import parece_inyeccion

log = logging.getLogger("cognia.lessons")

RUTA_POR_DEFECTO = Path(os.getenv("LECCIONES_DB", config.RAIZ / "data" / "lecciones.db"))
MAX_KEYTERMS = 50
MAX_EN_PROMPT = 15
TIPOS = ("keyterm", "sinonimo", "regla")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS lecciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo TEXT NOT NULL,
    contenido TEXT NOT NULL,
    origen TEXT NOT NULL,
    creada TEXT NOT NULL,
    UNIQUE (tipo, contenido)
)
"""

# "no, dije Palmira", "me refiero a Santa Marta", "quise decir Tuluá", "es Pereira, no Perea".
# Solo nombres propios (con mayúscula, como los transcribe el STT): son los que el STT confunde.
_NOMBRE = r"([A-ZÁÉÍÓÚÑ][\wáéíóúñü]+(?:\s+(?:de\s+|del\s+|la\s+)?[A-ZÁÉÍÓÚÑ][\wáéíóúñü]+){0,3})"
# "no X" a secas NO cuenta: en "Tuluá, no Tulua" lo negado es justo lo equivocado.
_CORRECCION = re.compile(rf"(?:\b[Nn]o\b[,.]?\s+(?:es\s+en|es|en|dije)\s+|\b[Dd]ije\s+|\b[Mm]e refiero a\s+|"
                         rf"\b[Qq]uise decir\s+|\b[Qq]uiero decir\s+){_NOMBRE}")
_NO_NOMBRES = {"No", "Sí", "Si", "Es", "El", "La", "Los", "Las", "Un", "Una", "Yo", "Que", "Qué", "Pero", "Gracias"}


def correcciones(texto: str) -> list[str]:
    """Nombres propios que el usuario repite para corregir al agente."""
    return [m.group(1) for m in _CORRECCION.finditer(texto or "") if m.group(1) not in _NO_NOMBRES][:3]


class Lecciones:
    def __init__(self, ruta: str | Path = RUTA_POR_DEFECTO):
        self.ruta = Path(ruta)
        self._candado = threading.Lock()
        self._lista = False
        self._cache: list[dict] | None = None  # se lee una vez y se actualiza al agregar

    def _con(self) -> sqlite3.Connection:
        if not self._lista:
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.ruta, timeout=5)
        con.row_factory = sqlite3.Row
        if not self._lista:
            con.execute(ESQUEMA)
            self._lista = True
        return con

    def todas(self) -> list[dict]:
        if self._cache is None:
            with self._candado:
                try:
                    con = self._con()
                    try:
                        filas = con.execute("SELECT tipo, contenido, origen, creada FROM lecciones ORDER BY id").fetchall()
                    finally:
                        con.close()
                    self._cache = [dict(f) for f in filas]
                except sqlite3.Error as e:
                    log.warning("No se pudieron leer las lecciones: %s", e)
                    return []
        return list(self._cache)

    def _agregar(self, tipo: str, contenido: str, origen: str) -> bool:
        with self._candado:
            con = self._con()
            try:
                cur = con.execute("INSERT OR IGNORE INTO lecciones (tipo, contenido, origen, creada) VALUES (?, ?, ?, ?)",
                                  (tipo, contenido, origen, datetime.now().isoformat(timespec="seconds")))
                con.commit()
                nueva = cur.rowcount == 1
            finally:
                con.close()
            if nueva and self._cache is not None:
                self._cache.append({"tipo": tipo, "contenido": contenido, "origen": origen})
            return nueva

    async def agregar(self, tipo: str, contenido: str, origen: str) -> ev.Lesson | None:
        """Guarda una lección. Devuelve el evento `lesson` si es nueva, None si ya existía o falló. Nunca lanza."""
        contenido = " ".join(str(contenido).split())[:300]
        if tipo not in TIPOS or not contenido:
            return None
        if parece_inyeccion(contenido):  # se agregaría al prompt de TODAS las sesiones: no se guarda
            log.warning("Lección descartada por parecer inyección de prompt (%s): %s", origen[:80], contenido[:120])
            return None
        try:
            nueva = await asyncio.to_thread(self._agregar, tipo, contenido, origen[:80])
        except sqlite3.Error as e:
            log.warning("No se pudo guardar la lección: %s", e)
            return None
        if nueva:
            log.info("Lección nueva (%s, %s): %s", tipo, origen, contenido)
            return ev.Lesson(kind=tipo, content=contenido, origin=origen[:80])
        return None

    def keyterms(self) -> list[str]:
        return [l["contenido"] for l in self.todas() if l["tipo"] == "keyterm"][-MAX_KEYTERMS:]

    def para_prompt(self) -> str:
        """Reglas y sinónimos aprendidos, para agregar al prompt de sistema ("" si no hay)."""
        utiles = [l for l in self.todas() if l["tipo"] in ("regla", "sinonimo")
                  and not parece_inyeccion(l["contenido"])][-MAX_EN_PROMPT:]  # defensa también al leer
        if not utiles:
            return ""
        lineas = [f"- {'Sinónimo' if l['tipo'] == 'sinonimo' else 'Regla'}: {l['contenido']}" for l in utiles]
        return "\n\nLECCIONES APRENDIDAS EN CONVERSACIONES ANTERIORES (aplícalas):\n" + "\n".join(lineas)

    def eventos(self, maximo: int = 10) -> list[ev.Lesson]:
        return [ev.Lesson(kind=l["tipo"], content=l["contenido"], origin=l["origen"]) for l in self.todas()[-maximo:]]
