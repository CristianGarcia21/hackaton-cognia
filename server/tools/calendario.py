"""Tools de calendario (issue #18, spec §8): el agente crea SOLO el evento de cada solicitud de cita.

    cal = HerramientasCalendario(citas)            # usa las solicitudes de server/tools/citas.py
    await cal.crear_evento_cita(solicitud_id=7)    # evento local (+ Google Calendar si está configurado)

Dos implementaciones detrás de la misma tool (CALENDAR_BACKEND, por defecto automático):
- local: SQLite (data/calendario.db) + feed GET /api/calendario.ics al que cualquier calendario se suscribe.
- google: además crea el evento en un Google Calendar real con una CUENTA DE SERVICIO (una API key no sirve:
  solo lee calendarios públicos). El dueño del calendario lo comparte con el correo de la cuenta de servicio
  ("Hacer cambios en eventos") y los eventos aparecen solos. Variables: GOOGLE_CALENDAR_ID y
  GOOGLE_SERVICE_ACCOUNT_JSON (el JSON de la cuenta, tal cual o en base64).

Honestidad: el título dice «Solicitud — pendiente de confirmación»; no es una cita confirmada por la IPS.
"""

import asyncio
import base64
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import jwt

from core.tools import Tool, tool
from server import config
from server.tools.citas import ESTADO_PENDIENTE, FRANJA_MIN, HerramientasCitas, _enmascarar, _fecha, _hora, enlace_google

log = logging.getLogger("cognia.calendario")

RUTA_POR_DEFECTO = Path(os.getenv("CALENDARIO_DB", config.RAIZ / "data" / "calendario.db"))
TITULO = "Solicitud de cita — pendiente de confirmación"
ZONA = "America/Bogota"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_EVENTOS = "https://www.googleapis.com/calendar/v3/calendars/{cal}/events"
TIMEOUT_GOOGLE_S = 4.0
TIMEOUT_GOOGLE_TOTAL_S = 5.0

ESQUEMA = """
CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    solicitud_id INTEGER NOT NULL UNIQUE,
    titulo TEXT NOT NULL,
    inicio TEXT NOT NULL,
    fin TEXT NOT NULL,
    sede TEXT,
    detalle TEXT,
    google_id TEXT,
    google_link TEXT,
    creado TEXT NOT NULL
)
"""


class EntradaInvalida(ValueError):
    pass


def _credenciales_google() -> dict | None:
    crudo = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not crudo or not os.getenv("GOOGLE_CALENDAR_ID"):
        return None
    try:
        return json.loads(crudo if crudo.startswith("{") else base64.b64decode(crudo).decode())
    except (ValueError, UnicodeDecodeError):
        log.error("GOOGLE_SERVICE_ACCOUNT_JSON no es un JSON válido (ni en base64): Google Calendar desactivado")
        return None


class GoogleCalendar:
    """Cliente mínimo de Google Calendar con cuenta de servicio (JWT firmado → token → POST del evento)."""

    def __init__(self, credenciales: dict, calendario: str):
        self.cred, self.calendario = credenciales, calendario
        self._token: tuple[str, float] | None = None

    @property
    def correo(self) -> str:
        return self.cred.get("client_email", "")

    async def _acceso(self, cliente: httpx.AsyncClient) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        ahora = int(time.time())
        firma = jwt.encode({"iss": self.correo, "scope": "https://www.googleapis.com/auth/calendar.events",
                            "aud": GOOGLE_TOKEN, "iat": ahora, "exp": ahora + 3600},
                           self.cred["private_key"], algorithm="RS256")
        r = await cliente.post(GOOGLE_TOKEN, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                                   "assertion": firma})
        r.raise_for_status()
        datos = r.json()
        self._token = (datos["access_token"], time.time() + int(datos.get("expires_in", 3600)))
        return self._token[0]

    async def crear(self, titulo: str, inicio: datetime, fin: datetime, descripcion: str, lugar: str) -> tuple[str, str]:
        async with httpx.AsyncClient(timeout=TIMEOUT_GOOGLE_S) as cliente:
            token = await self._acceso(cliente)
            cuerpo = {"summary": titulo, "description": descripcion, "location": lugar,
                      "start": {"dateTime": inicio.isoformat(), "timeZone": ZONA},
                      "end": {"dateTime": fin.isoformat(), "timeZone": ZONA},
                      "reminders": {"useDefault": True}}
            r = await cliente.post(GOOGLE_EVENTOS.format(cal=self.calendario), json=cuerpo,
                                   headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            d = r.json()
            return d.get("id", ""), d.get("htmlLink", "")


class HerramientasCalendario:
    def __init__(self, citas: HerramientasCitas, ruta: str | Path = RUTA_POR_DEFECTO,
                 google: GoogleCalendar | None | bool = True):
        self.citas, self.ruta = citas, Path(ruta)
        if google is True:  # automático: Google solo si hay credenciales y CALENDAR_BACKEND no es "local"
            cred = None if os.getenv("CALENDAR_BACKEND", "").lower() == "local" else _credenciales_google()
            google = GoogleCalendar(cred, os.environ["GOOGLE_CALENDAR_ID"]) if cred else None
        self.google: GoogleCalendar | None = google or None
        self._candado = threading.Lock()
        self._lista = False

    @property
    def backend(self) -> str:
        return "google" if self.google else "local"

    @contextmanager
    def _conexion(self):
        with self._candado:
            if not self._lista:
                self.ruta.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(self.ruta, timeout=5)
            try:
                con.row_factory = sqlite3.Row
                if not self._lista:
                    con.execute(ESQUEMA)
                    self._lista = True
                yield con
                con.commit()
            finally:
                con.close()

    def eventos(self) -> list[dict]:
        with self._conexion() as con:
            return [dict(f) for f in con.execute("SELECT * FROM eventos ORDER BY inicio").fetchall()]

    def _evento_de(self, solicitud_id: int) -> dict | None:
        with self._conexion() as con:
            f = con.execute("SELECT * FROM eventos WHERE solicitud_id = ?", (solicitud_id,)).fetchone()
        return dict(f) if f else None

    def _guardar(self, datos: dict) -> dict:
        with self._conexion() as con:
            con.execute("INSERT OR IGNORE INTO eventos (solicitud_id, titulo, inicio, fin, sede, detalle, google_id, "
                        "google_link, creado) VALUES (:solicitud_id, :titulo, :inicio, :fin, :sede, :detalle, "
                        ":google_id, :google_link, :creado)", datos | {"creado": datetime.now().isoformat(timespec="seconds")})
        return self._evento_de(datos["solicitud_id"])

    # ------------------------------- tools -------------------------------

    async def crear_evento_cita(self, solicitud_id: int) -> str:
        """Crea el evento de calendario de una solicitud de cita ya registrada (su número #). Llámala SIEMPRE justo después de que registrar_solicitud_cita confirme el registro, sin preguntarle al usuario: el evento queda en el calendario automáticamente.

        Args:
            solicitud_id: número de la solicitud que devolvió registrar_solicitud_cita (ej. 7 para "#7")
        """
        try:
            sid = int(str(solicitud_id).lstrip("#").strip())
        except ValueError:
            return f"Error: '{solicitud_id}' no es un número de solicitud. Usa el # que devolvió registrar_solicitud_cita."
        try:
            return await self._crear(sid)
        except EntradaInvalida as e:
            return f"Error: {e}"
        except sqlite3.Error:
            log.exception("Fallo de SQLite en calendario")
            return "Error: no pude guardar el evento en este momento. Intenta de nuevo."

    async def _crear(self, sid: int) -> str:
        s = await asyncio.to_thread(self.citas.solicitud, sid)
        if s is None:
            raise EntradaInvalida(f"no existe la solicitud #{sid}")
        if not s.get("fecha_hora"):
            raise EntradaInvalida(f"la solicitud #{sid} no tiene día y hora exactos; no se puede poner en el calendario")
        previo = await asyncio.to_thread(self._evento_de, sid)
        if previo:
            return self._texto(previo, s, nuevo=False)
        inicio = datetime.fromisoformat(s["fecha_hora"])
        fin = inicio + timedelta(minutes=FRANJA_MIN)
        sede = s.get("sede_nombre") or f"sede {s['sede_codigo']}"
        def detalle_con(paciente: str) -> str:
            return (f"Paciente: {paciente}. Motivo: {s['motivo']}. Sede {s['sede_codigo']}. Estado: "
                    f"{ESTADO_PENDIENTE}. Registrada por Kognia con datos de datos.gov.co.")
        detalle = detalle_con(_enmascarar(s["paciente"]))  # el feed .ics es público: nombre abreviado
        google_id = google_link = None
        if self.google:
            try:
                # Tope total menor al del hub (8 s): si Google tarda, igual queda el evento local.
                google_id, google_link = await asyncio.wait_for(self.google.crear(
                    f"{TITULO} · {sede}", inicio, fin, detalle_con(s["paciente"]), sede), TIMEOUT_GOOGLE_TOTAL_S)
            except Exception as e:  # noqa: BLE001 — Google caído o sin permisos: queda el evento local
                log.warning("Google Calendar no disponible (%s: %s)", type(e).__name__, str(e)[:200])
        evento = await asyncio.to_thread(self._guardar, {
            "solicitud_id": sid, "titulo": f"{TITULO} · {sede}", "inicio": inicio.isoformat(timespec="minutes"),
            "fin": fin.isoformat(timespec="minutes"), "sede": sede, "detalle": detalle,
            "google_id": google_id, "google_link": google_link})
        return self._texto(evento, s, nuevo=True)

    def _texto(self, evento: dict, s: dict, nuevo: bool) -> str:
        donde = ("en Google Calendar" if evento.get("google_link")
                 else "en el calendario de Kognia (también en /api/calendario.ics)")
        inicio = datetime.fromisoformat(evento["inicio"])
        return (f"{'Evento creado' if nuevo else 'El evento ya existía'} {donde} para la solicitud #{s['id']}: "
                f"{_fecha(inicio)} a las {_hora(inicio)} en {evento['sede']}, titulado «{TITULO}». "
                "Díselo al usuario en una frase; recuerda que la IPS debe confirmar."
                + (f" Enlace: {evento['google_link']}" if evento.get("google_link") else ""))

    def tools(self) -> list[Tool]:
        return [tool(self.crear_evento_cita)]


def feed_ics(eventos: list[dict]) -> str:
    """Todos los eventos como un calendario .ics (suscribible desde Google, Outlook o Apple)."""
    def esc(t: str) -> str:
        return (t or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

    def fmt(iso: str) -> str:
        return datetime.fromisoformat(iso).strftime("%Y%m%dT%H%M%S")

    lineas = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Kognia//Calendario//ES", "X-WR-CALNAME:Kognia · Solicitudes",
              f"X-WR-TIMEZONE:{ZONA}"]
    for e in eventos:
        lineas += ["BEGIN:VEVENT", f"UID:evento-{e['solicitud_id']}@kognia",
                   f"DTSTAMP:{fmt(e['creado'])}", f"DTSTART;TZID={ZONA}:{fmt(e['inicio'])}",
                   f"DTEND;TZID={ZONA}:{fmt(e['fin'])}", f"SUMMARY:{esc(e['titulo'])}",
                   f"LOCATION:{esc(e.get('sede') or '')}", f"DESCRIPTION:{esc(e.get('detalle') or '')}", "END:VEVENT"]
    return "\r\n".join(lineas + ["END:VCALENDAR", ""])


__all__ = ["HerramientasCalendario", "GoogleCalendar", "feed_ics", "enlace_google", "TITULO"]
