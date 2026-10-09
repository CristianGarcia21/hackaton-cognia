"""Tools de solicitudes de cita (issue #16, spec §8): la «integración con base de datos» del agente.

    citas = HerramientasCitas()                      # SQLite en data/citas.db (o CITAS_DB)
    await citas.registrar_solicitud_cita(paciente="María Gómez", sede_codigo="500102126-01", motivo="cirugía")
    await citas.listar_solicitudes(paciente="maria")
    citas.solicitudes()                              # filas como dict: las usan Excel (#17) y Calendario (#18)

Honestidad: el dataset de IPS NO tiene agenda. Lo que se registra es una SOLICITUD con estado
«pendiente de confirmación por la IPS», nunca una cita confirmada, y el texto de la tool lo dice para que
el agente lo repita.

Contrato Tool (como server/tools/ips.py): devuelven texto, nunca lanzan; los errores vuelven como
"Error: ..." para que el LLM se corrija. SQLite corre en un hilo (asyncio.to_thread) para no frenar la voz;
la base se crea en la primera escritura o lectura (importar o listar las tools no toca el disco).
"""

import asyncio
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.tools import Tool, tool
from server import config

log = logging.getLogger("cognia.citas")

ESTADO_PENDIENTE = "pendiente de confirmación por la IPS"
RUTA_POR_DEFECTO = Path(os.getenv("CITAS_DB", config.RAIZ / "data" / "citas.db"))
VENTANA_DUPLICADO_S = 120  # un voice agent puede repetir la llamada: misma solicitud en 2 min = la misma
MAX_LISTADO = 10
SEDE_ID = re.compile(r"^\d{5,15}-\d{1,3}$")  # c_digo_sede-n_mero_sede, como lo devuelve buscar_ips

ESQUEMA = """
CREATE TABLE IF NOT EXISTS solicitudes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paciente TEXT NOT NULL,
    paciente_norm TEXT NOT NULL,
    documento TEXT,
    telefono TEXT,
    sede_codigo TEXT NOT NULL,
    sede_nombre TEXT,
    motivo TEXT NOT NULL,
    fecha_preferida TEXT,
    estado TEXT NOT NULL,
    creada TEXT NOT NULL
)
"""


class EntradaInvalida(ValueError):
    """Dato del usuario que no sirve para registrar: el mensaje va tal cual al LLM."""


def _txt(v) -> str:
    return "" if v is None else " ".join(str(v).split())


def _norm(v: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", v).encode("ascii", "ignore").decode()
    return " ".join(sin_tildes.lower().split())


class HerramientasCitas:
    def __init__(self, ruta: str | Path = RUTA_POR_DEFECTO):
        self.ruta = Path(ruta)
        self._lista = False
        self._candado = threading.Lock()  # una escritura a la vez (SQLite + varias sesiones)

    # ------------------------------- base de datos -------------------------------

    @contextmanager
    def _conexion(self):
        """Una conexión por operación (corre en un hilo de to_thread): abre, confirma y SIEMPRE cierra."""
        with self._candado:
            if not self._lista:
                self.ruta.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(self.ruta, timeout=5)
            try:
                con.row_factory = sqlite3.Row
                if not self._lista:
                    con.execute("PRAGMA journal_mode=WAL")
                    con.execute(ESQUEMA)
                    self._lista = True
                yield con
                con.commit()
            finally:
                con.close()

    def solicitudes(self, paciente: str = "") -> list[dict]:
        """Solicitudes como dict (más recientes primero). Para Excel (#17), Calendario (#18) y la UI."""
        with self._conexion() as con:
            if paciente:
                # % y _ del usuario son texto, no comodines de LIKE.
                patron = _norm(paciente).replace("!", "!!").replace("%", "!%").replace("_", "!_")
                filas = con.execute("SELECT * FROM solicitudes WHERE paciente_norm LIKE ? ESCAPE '!' "
                                    "ORDER BY id DESC", (f"%{patron}%",)).fetchall()
            else:
                filas = con.execute("SELECT * FROM solicitudes ORDER BY id DESC").fetchall()
        return [{k: f[k] for k in f.keys() if k != "paciente_norm"} for f in filas]

    def solicitud(self, solicitud_id: int) -> dict | None:
        with self._conexion() as con:
            f = con.execute("SELECT * FROM solicitudes WHERE id = ?", (solicitud_id,)).fetchone()
        return None if f is None else {k: f[k] for k in f.keys() if k != "paciente_norm"}

    def _insertar(self, datos: dict) -> tuple[int, bool]:
        """Inserta y devuelve (id, nueva). Si es la misma solicitud de hace un momento, no la duplica."""
        with self._conexion() as con:
            limite = datetime.fromtimestamp(time.time() - VENTANA_DUPLICADO_S).isoformat(timespec="seconds")
            previa = con.execute(
                "SELECT id FROM solicitudes WHERE paciente_norm = ? AND sede_codigo = ? AND motivo = ? "
                "AND COALESCE(fecha_preferida, '') = ? AND creada >= ? ORDER BY id DESC LIMIT 1",
                (datos["paciente_norm"], datos["sede_codigo"], datos["motivo"], datos["fecha_preferida"] or "",
                 limite)).fetchone()
            if previa:
                return previa["id"], False
            cur = con.execute(
                "INSERT INTO solicitudes (paciente, paciente_norm, documento, telefono, sede_codigo, sede_nombre, "
                "motivo, fecha_preferida, estado, creada) VALUES (:paciente, :paciente_norm, :documento, "
                ":telefono, :sede_codigo, :sede_nombre, :motivo, :fecha_preferida, :estado, :creada)",
                datos | {"estado": ESTADO_PENDIENTE, "creada": datetime.now().isoformat(timespec="seconds")})
            return cur.lastrowid, True

    # ------------------------------- tools -------------------------------

    async def _seguro(self, funcion, *args) -> str:
        """Corre la parte de SQLite en un hilo y convierte toda falla en texto (contrato Tool)."""
        try:
            return await asyncio.to_thread(funcion, *args)
        except EntradaInvalida as e:
            return f"Error: {e}"
        except sqlite3.Error:
            log.exception("Fallo de SQLite en citas")
            return "Error: no pude guardar ni leer las solicitudes en este momento. Intenta de nuevo."

    async def registrar_solicitud_cita(self, paciente: str, sede_codigo: str, motivo: str, documento: str = "",
                                       telefono: str = "", fecha_preferida: str = "", sede_nombre: str = "") -> str:
        """Registra una SOLICITUD de cita en una sede de IPS. No es una cita confirmada: queda pendiente de confirmación por la IPS, y debes decírselo al usuario. Úsala solo cuando el usuario ya eligió la sede (con su id de buscar_ips o detalle_ips) y pidió registrar la solicitud.

        Args:
            paciente: nombre completo del paciente
            sede_codigo: id de la sede tal como lo dio buscar_ips o detalle_ips (ej. "500102126-01")
            motivo: motivo de la cita en pocas palabras (ej. "valoración para cirugía")
            documento: número de documento del paciente, si lo dio
            telefono: teléfono de contacto, si lo dio
            fecha_preferida: fecha u horario que prefiere, en sus palabras (ej. "el lunes en la mañana")
            sede_nombre: nombre de la sede, para mostrarlo en el registro
        """
        return await self._seguro(self._registrar, paciente, sede_codigo, motivo, documento, telefono,
                                  fecha_preferida, sede_nombre)

    def _registrar(self, paciente, sede_codigo, motivo, documento, telefono, fecha_preferida, sede_nombre) -> str:
        paciente, sede_codigo, motivo = _txt(paciente), _txt(sede_codigo).replace(" ", ""), _txt(motivo)
        documento, telefono = _txt(documento), _txt(telefono)
        fecha_preferida, sede_nombre = _txt(fecha_preferida), _txt(sede_nombre)
        faltan = [n for n, v in (("el nombre del paciente", paciente), ("el id de la sede", sede_codigo),
                                 ("el motivo", motivo)) if not v]
        if faltan:
            raise EntradaInvalida(f"falta {' y '.join(faltan)}. Pídeselo al usuario antes de registrar.")
        if not SEDE_ID.match(sede_codigo):
            raise EntradaInvalida(f"'{sede_codigo}' no es un id de sede válido. Usa el id que devolvió "
                                  "buscar_ips o detalle_ips (formato 500102126-01); no lo inventes.")
        if telefono and not 7 <= len(re.sub(r"\D", "", telefono)) <= 13:
            raise EntradaInvalida(f"el teléfono '{telefono}' no parece válido. Confírmalo con el usuario.")
        if len(paciente) > 120 or len(motivo) > 300:
            raise EntradaInvalida("el nombre o el motivo son demasiado largos; resúmelos.")

        solicitud_id, nueva = self._insertar({
            "paciente": paciente, "paciente_norm": _norm(paciente), "documento": documento or None,
            "telefono": telefono or None, "sede_codigo": sede_codigo, "sede_nombre": sede_nombre or None,
            "motivo": motivo, "fecha_preferida": fecha_preferida or None,
        })
        sede = f"{sede_nombre} (id {sede_codigo})" if sede_nombre else f"la sede {sede_codigo}"
        cuando = f", fecha preferida: {fecha_preferida}" if fecha_preferida else ""
        inicio = (f"Solicitud #{solicitud_id} registrada" if nueva
                  else f"La solicitud #{solicitud_id} ya estaba registrada (no la dupliqué)")
        return (f"{inicio} para {paciente} en {sede}. Motivo: {motivo}{cuando}. Estado: {ESTADO_PENDIENTE}. "
                "Dile al usuario que NO es una cita confirmada: la IPS debe contactarlo para confirmar fecha y "
                "hora, porque estos datos no incluyen la agenda de las IPS.")

    async def listar_solicitudes(self, paciente: str = "") -> str:
        """Lista las solicitudes de cita registradas (las más recientes primero), con su estado. Úsala cuando el usuario pregunte por sus solicitudes o quiera confirmar qué quedó registrado.

        Args:
            paciente: nombre (o parte del nombre) del paciente; vacío para listar todas
        """
        return await self._seguro(self._listar, paciente)

    def _listar(self, paciente) -> str:
        paciente = _txt(paciente)
        filas = self.solicitudes(paciente)
        if not filas:
            return (f"No hay solicitudes registradas para '{paciente}'." if paciente
                    else "Todavía no hay solicitudes de cita registradas.")
        lineas = [f"{len(filas)} solicitud{'es' if len(filas) != 1 else ''}"
                  + (f" de '{paciente}'" if paciente else "") + ":"]
        for f in filas[:MAX_LISTADO]:
            sede = f["sede_nombre"] or f"sede {f['sede_codigo']}"
            cuando = f" · prefiere {f['fecha_preferida']}" if f["fecha_preferida"] else ""
            lineas.append(f"#{f['id']} · {f['paciente']} · {sede} · {f['motivo']}{cuando} · {f['estado']}")
        if len(filas) > MAX_LISTADO:
            lineas.append(f"… y {len(filas) - MAX_LISTADO} más.")
        return "\n".join(lineas)

    def tools(self) -> list[Tool]:
        return [tool(self.registrar_solicitud_cita), tool(self.listar_solicitudes)]
