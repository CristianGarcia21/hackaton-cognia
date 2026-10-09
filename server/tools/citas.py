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
import json
from datetime import date, datetime, timedelta
from urllib.parse import quote
from pathlib import Path

from core.tools import Tool, tool
from server import config
from server.tools import fechas

log = logging.getLogger("cognia.citas")

ESTADO_PENDIENTE = "pendiente de confirmación por la IPS"
RUTA_POR_DEFECTO = Path(os.getenv("CITAS_DB", config.RAIZ / "data" / "citas.db"))
VENTANA_DUPLICADO_S = 120  # un voice agent puede repetir la llamada: misma solicitud en 2 min = la misma
MAX_LISTADO = 10
SEDE_ID = re.compile(r"^\d{5,15}-\d{1,3}$")  # c_digo_sede-n_mero_sede, como lo devuelve buscar_ips

# Agenda PROPIA de solicitudes (el dataset no tiene agenda): franjas de 30 min por sede, de lunes a sábado.
FRANJA_MIN = 30
HORA_APERTURA, HORA_CIERRE = 7, 18
MAX_LIBRES = 3
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]
SEMILLAS = config.RAIZ / "data" / "semillas" / "citas.json"

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
    creada TEXT NOT NULL,
    fecha_hora TEXT
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
                    columnas = {f[1] for f in con.execute("PRAGMA table_info(solicitudes)")}
                    if "fecha_hora" not in columnas:  # bases creadas antes de la agenda por franjas
                        con.execute("ALTER TABLE solicitudes ADD COLUMN fecha_hora TEXT")
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
        """Inserta y devuelve (id, nueva). Si es la misma solicitud de hace un momento, no la duplica. Si la
        franja de esa sede ya está tomada, lanza EntradaInvalida con las franjas libres más cercanas (en la
        misma transacción: dos sesiones no pueden quedarse con la misma franja)."""
        with self._conexion() as con:
            limite = datetime.fromtimestamp(time.time() - VENTANA_DUPLICADO_S).isoformat(timespec="seconds")
            previa = con.execute(
                "SELECT id FROM solicitudes WHERE paciente_norm = ? AND sede_codigo = ? AND motivo = ? "
                "AND COALESCE(fecha_preferida, '') = ? AND COALESCE(fecha_hora, '') = ? AND creada >= ? "
                "ORDER BY id DESC LIMIT 1",
                (datos["paciente_norm"], datos["sede_codigo"], datos["motivo"], datos["fecha_preferida"] or "",
                 datos.get("fecha_hora") or "", limite)).fetchone()
            if previa:
                return previa["id"], False
            if datos.get("fecha_hora"):
                ocupada = con.execute("SELECT id FROM solicitudes WHERE sede_codigo = ? AND fecha_hora = ?",
                                      (datos["sede_codigo"], datos["fecha_hora"])).fetchone()
                if ocupada:
                    franja = datetime.fromisoformat(datos["fecha_hora"])
                    libres = _libres(_tomadas(con, datos["sede_codigo"], franja.date()), franja)
                    raise EntradaInvalida(
                        f"la franja {_hora(franja)} del {_fecha(franja)} ya tiene una solicitud en esa sede. "
                        + (f"Franjas libres cercanas: {', '.join(_hora(x) for x in libres)}. Ofrécelas al usuario."
                           if libres else "No quedan franjas libres ese día; ofrece otro día."))
            cur = con.execute(
                "INSERT INTO solicitudes (paciente, paciente_norm, documento, telefono, sede_codigo, sede_nombre, "
                "motivo, fecha_preferida, estado, creada, fecha_hora) VALUES (:paciente, :paciente_norm, "
                ":documento, :telefono, :sede_codigo, :sede_nombre, :motivo, :fecha_preferida, :estado, :creada, "
                ":fecha_hora)",
                {"fecha_hora": None} | datos
                | {"estado": ESTADO_PENDIENTE, "creada": datetime.now().isoformat(timespec="seconds")})
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
                                       telefono: str = "", fecha_preferida: str = "", sede_nombre: str = "",
                                       fecha_hora: str = "") -> str:
        """Registra una SOLICITUD de cita en una sede de IPS. No es una cita confirmada: queda pendiente de confirmación por la IPS, y debes decírselo al usuario. Úsala solo cuando el usuario ya eligió la sede (con su id de buscar_ips o detalle_ips), te dio fecha y hora, y CONFIRMÓ los datos. Si la franja está ocupada, la tool te da las libres: ofrécelas.

        Args:
            paciente: nombre completo del paciente
            sede_codigo: id de la sede tal como lo dio buscar_ips o detalle_ips (ej. "500102126-01")
            motivo: motivo de la cita en pocas palabras (ej. "valoración para cirugía")
            documento: número de documento del paciente, si lo dio
            telefono: teléfono de contacto, si lo dio
            fecha_preferida: fecha u horario que prefiere, en sus palabras (ej. "el lunes en la mañana")
            sede_nombre: nombre de la sede, para mostrarlo en el registro
            fecha_hora: día y hora TAL COMO LOS DIJO el usuario (ej. "el lunes 12 a las 10 y media", "mañana a las 3 de la tarde"); no los conviertas tú, la tool los interpreta. Franjas de 30 min, de lunes a sábado entre 7:00 y 18:00
        """
        return await self._seguro(self._registrar, paciente, sede_codigo, motivo, documento, telefono,
                                  fecha_preferida, sede_nombre, fecha_hora)

    def _registrar(self, paciente, sede_codigo, motivo, documento, telefono, fecha_preferida, sede_nombre,
                   fecha_hora="") -> str:
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
        franja = _franja(fecha_hora) if _txt(fecha_hora) else None

        solicitud_id, nueva = self._insertar({
            "paciente": paciente, "paciente_norm": _norm(paciente), "documento": documento or None,
            "telefono": telefono or None, "sede_codigo": sede_codigo, "sede_nombre": sede_nombre or None,
            "motivo": motivo, "fecha_preferida": fecha_preferida or None,
            "fecha_hora": franja.isoformat(timespec="minutes") if franja else None,
        })
        sede = f"{sede_nombre} (id {sede_codigo})" if sede_nombre else f"la sede {sede_codigo}"
        cuando = (f", para el {_fecha(franja)} a las {_hora(franja)}" if franja
                  else f", fecha preferida: {fecha_preferida}" if fecha_preferida else "")
        inicio = (f"Solicitud #{solicitud_id} registrada" if nueva
                  else f"La solicitud #{solicitud_id} ya estaba registrada (no la dupliqué)")
        return (f"{inicio} para {paciente} en {sede}. Motivo: {motivo}{cuando}. Estado: {ESTADO_PENDIENTE}. "
                "Dile al usuario que NO es una cita confirmada: la IPS debe contactarlo para confirmarla, "
                "porque estos datos no incluyen la agenda de las IPS. Queda en la lista de solicitudes (/citas), "
                f"lista para enviarse a la IPS. Ahora llama crear_evento_cita con solicitud_id={solicitud_id} "
                "para ponerla en el calendario (sin preguntar).")

    async def proponer_cita(self, paciente: str, sede_codigo: str, motivo: str, fecha_hora: str,
                            sede_nombre: str = "") -> str:
        """Prepara una solicitud de cita para que el usuario la CONFIRME: valida la fecha y la hora, revisa que la franja esté libre y muestra en pantalla la ficha de la cita. Llámala siempre ANTES de pedirle al usuario el "sí"; si devuelve franjas libres, ofrécelas. No registra nada.

        Args:
            paciente: nombre completo del paciente
            sede_codigo: id de la sede tal como lo dio buscar_ips o detalle_ips (ej. "500102126-01")
            motivo: motivo de la cita en pocas palabras
            fecha_hora: día y hora TAL COMO LOS DIJO el usuario (ej. "el lunes 12 a las 10 y media"); la tool los interpreta
            sede_nombre: nombre de la sede, para la ficha
        """
        return await self._seguro(self._proponer, paciente, sede_codigo, motivo, fecha_hora, sede_nombre)

    def _proponer(self, paciente, sede_codigo, motivo, fecha_hora, sede_nombre) -> str:
        paciente, sede_codigo, motivo = _txt(paciente), _txt(sede_codigo).replace(" ", ""), _txt(motivo)
        faltan = [n for n, v in (("el nombre del paciente", paciente), ("el motivo", motivo),
                                 ("el día y la hora", _txt(fecha_hora))) if not v]
        if faltan:
            raise EntradaInvalida(f"falta {' y '.join(faltan)}. Pídeselo al usuario.")
        if not SEDE_ID.match(sede_codigo):
            raise EntradaInvalida(f"'{sede_codigo}' no es un id de sede válido; usa el de buscar_ips o detalle_ips.")
        franja = _franja(fecha_hora)
        with self._conexion() as con:
            tomadas = _tomadas(con, sede_codigo, franja.date())
        if franja in tomadas:
            libres = _libres(tomadas, franja)
            raise EntradaInvalida(
                f"la franja {_hora(franja)} del {_fecha(franja)} ya tiene una solicitud en esa sede. "
                + (f"Franjas libres cercanas: {', '.join(_hora(x) for x in libres)}. Ofrécelas al usuario."
                   if libres else "No quedan franjas libres ese día; ofrece otro día."))
        sede = _txt(sede_nombre) or f"la sede {sede_codigo}"
        return (f"Propuesta lista (franja libre): {paciente} en {sede}, el {_fecha(franja)} de {franja.year} a las "
                f"{_hora(franja)}, motivo {motivo}. La ficha ya está en pantalla. Léele estos datos al usuario y "
                "pregúntale si confirma; con su «sí», llama registrar_solicitud_cita con los mismos datos.")

    async def horarios_ocupados(self, sede_codigo: str, fecha: str) -> str:
        """Consulta la agenda de SOLICITUDES ya registradas en una sede para un día: qué franjas están tomadas y cuáles libres. Úsala antes de proponer una hora o cuando el usuario pregunte qué horarios hay.

        Args:
            sede_codigo: id de la sede tal como lo dio buscar_ips o detalle_ips (ej. "500102126-01")
            fecha: día TAL COMO LO DIJO el usuario (ej. "el lunes 12", "mañana", "15 de octubre"); la tool lo interpreta
        """
        return await self._seguro(self._horarios, sede_codigo, fecha)

    def _horarios(self, sede_codigo, fecha) -> str:
        sede_codigo = _txt(sede_codigo).replace(" ", "")
        if not SEDE_ID.match(sede_codigo):
            raise EntradaInvalida(f"'{sede_codigo}' no es un id de sede válido; usa el de buscar_ips o detalle_ips.")
        try:
            dia = fechas.interpretar_dia(_txt(fecha))
        except fechas.FechaNoEntendida as e:
            raise EntradaInvalida(f"{e}. Pídele al usuario el día.") from None
        with self._conexion() as con:
            tomadas = _tomadas(con, sede_codigo, dia)
        inicio = datetime.combine(dia, datetime.min.time()).replace(hour=HORA_APERTURA)
        libres = _libres(tomadas, inicio, maximo=6)
        ocupadas = ", ".join(_hora(t) for t in sorted(tomadas)) or "ninguna"
        return (f"Agenda de solicitudes de la sede {sede_codigo} el {_fecha(inicio)} de {inicio.year} "
                f"(usa ESTA fecha al confirmar): ocupadas {ocupadas}. "
                + (f"Primeras libres: {', '.join(_hora(x) for x in libres)}." if libres else "No quedan franjas libres.")
                + " Son solicitudes registradas aquí, no la agenda real de la IPS.")

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
        return [tool(self.proponer_cita), tool(self.registrar_solicitud_cita), tool(self.listar_solicitudes),
                tool(self.horarios_ocupados)]

    # ------------------------------- semillas (demo) -------------------------------

    def sembrar(self, ruta: Path = SEMILLAS) -> int:
        """Si no hay solicitudes, carga las de ejemplo (pacientes ficticios, sedes reales del dataset). El disco
        de Render se borra en cada despliegue: así /citas, el Excel y el calendario nunca arrancan vacíos."""
        if not ruta.exists() or self.solicitudes():
            return 0
        n = 0
        for s in json.loads(ruta.read_text(encoding="utf-8")):
            dia = _siguiente_habil(date.today() + timedelta(days=int(s.get("en_dias", 1))))
            cuando = f"{dia.isoformat()}T{s['hora']}"
            try:
                self._registrar(s["paciente"], s["sede_codigo"], s["motivo"], "", "", "", s.get("sede_nombre", ""),
                                cuando)
                n += 1
            except EntradaInvalida as e:
                log.warning("Semilla de cita omitida: %s", e)
        return n


# ------------------------------- agenda por franjas -------------------------------

def _franja(texto: str) -> datetime:
    """Fecha y hora del LLM → inicio de su franja de 30 min. Valida que sea futura y en horario."""
    try:
        dt = fechas.interpretar(_txt(texto))
    except fechas.FechaNoEntendida as e:
        raise EntradaInvalida(f"{e}. Pídele al usuario el día y la hora (ej. «el lunes 12 a las 10 y media»).") from None
    dt = dt.replace(tzinfo=None, second=0, microsecond=0)
    dt -= timedelta(minutes=dt.minute % FRANJA_MIN)
    if dt < datetime.now():
        raise EntradaInvalida(f"el {_fecha(dt)} a las {_hora(dt)} ya pasó; pide otra fecha.")
    if dt.weekday() == 6 or not HORA_APERTURA <= dt.hour < HORA_CIERRE:
        raise EntradaInvalida(f"las solicitudes se agendan de lunes a sábado entre las {HORA_APERTURA}:00 y las "
                              f"{HORA_CIERRE}:00; propón otro horario.")
    return dt


def _tomadas(con, sede_codigo: str, dia: date) -> set[datetime]:
    filas = con.execute("SELECT fecha_hora FROM solicitudes WHERE sede_codigo = ? AND fecha_hora LIKE ?",
                        (sede_codigo, f"{dia.isoformat()}%")).fetchall()
    return {datetime.fromisoformat(f[0]) for f in filas if f[0]}


def _libres(tomadas: set[datetime], cerca_de: datetime, maximo: int = MAX_LIBRES) -> list[datetime]:
    """Franjas libres del mismo día, las más cercanas a la pedida primero (y en orden de hora)."""
    dia = cerca_de.replace(hour=HORA_APERTURA, minute=0)
    todas = [dia + timedelta(minutes=FRANJA_MIN * i) for i in range((HORA_CIERRE - HORA_APERTURA) * 60 // FRANJA_MIN)]
    libres = [f for f in todas if f not in tomadas and f > datetime.now()]
    return sorted(sorted(libres, key=lambda f: abs(f - cerca_de))[:maximo])


def _siguiente_habil(d: date) -> date:
    return d + timedelta(days=1) if d.weekday() == 6 else d


def _fecha(dt: datetime) -> str:
    return f"{DIAS[dt.weekday()]} {dt.day} de {MESES[dt.month - 1]}"


def _hora(dt: datetime) -> str:
    return dt.strftime("%H:%M")


# ------------------------------- vista pública y calendario -------------------------------

def _enmascarar(nombre: str) -> str:
    """'María Gómez Ruiz' → 'María G.': la página /citas es pública; los datos completos van solo al Excel."""
    partes = _txt(nombre).split()
    return " ".join(partes[:1] + [f"{p[0]}." for p in partes[1:2]]) if partes else ""


def _titulo(s: dict) -> str:
    return f"Solicitud de cita ({ESTADO_PENDIENTE}) · {s.get('sede_nombre') or 'sede ' + s['sede_codigo']}"


def _rango(s: dict) -> tuple[datetime, datetime] | None:
    if not s.get("fecha_hora"):
        return None
    inicio = datetime.fromisoformat(s["fecha_hora"])
    return inicio, inicio + timedelta(minutes=FRANJA_MIN)


def enlace_google(s: dict) -> str | None:
    """Enlace "Agregar a Google Calendar": abre el calendario de quien esté logueado, sin OAuth ni keys."""
    if (r := _rango(s)) is None:
        return None
    fechas = "/".join(x.strftime("%Y%m%dT%H%M00") for x in r)
    detalle = f"Motivo: {s['motivo']}. Sede {s['sede_codigo']}. Estado: {ESTADO_PENDIENTE}. Fuente: Kognia."
    return ("https://calendar.google.com/calendar/render?action=TEMPLATE"
            f"&text={quote(_titulo(s))}&dates={fechas}&ctz=America/Bogota&details={quote(detalle)}")


def ics(s: dict) -> str | None:
    if (r := _rango(s)) is None:
        return None
    def esc(t: str) -> str:
        return t.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Kognia//Solicitudes//ES", "BEGIN:VEVENT",
        f"UID:solicitud-{s['id']}@kognia", f"DTSTAMP:{datetime.now().strftime('%Y%m%dT%H%M%S')}",
        f"DTSTART;TZID=America/Bogota:{r[0].strftime('%Y%m%dT%H%M%S')}",
        f"DTEND;TZID=America/Bogota:{r[1].strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{esc(_titulo(s))}", f"DESCRIPTION:{esc('Motivo: ' + s['motivo'] + '. ' + ESTADO_PENDIENTE)}",
        "END:VEVENT", "END:VCALENDAR", ""])


def vista_publica(filas: list[dict]) -> dict:
    """Solicitudes agrupadas por sede, listas para enviar a cada IPS, sin documento ni teléfono."""
    grupos: dict[str, dict] = {}
    for s in sorted(filas, key=lambda f: (f.get("fecha_hora") or "9999", f["id"])):
        g = grupos.setdefault(s["sede_codigo"], {"sede_codigo": s["sede_codigo"],
                                                 "sede_nombre": s.get("sede_nombre") or "", "solicitudes": []})
        g["sede_nombre"] = g["sede_nombre"] or s.get("sede_nombre") or ""
        inicio = datetime.fromisoformat(s["fecha_hora"]) if s.get("fecha_hora") else None
        g["solicitudes"].append({
            "id": s["id"], "paciente": _enmascarar(s["paciente"]), "motivo": s["motivo"],
            "cuando": f"{_fecha(inicio)} · {_hora(inicio)}" if inicio else (s.get("fecha_preferida") or "sin fecha"),
            "fecha_hora": s.get("fecha_hora"), "estado": s["estado"], "creada": s["creada"],
            "google": enlace_google(s), "ics": f"/api/citas/{s['id']}.ics" if inicio else None})
    return {"total": len(filas), "grupos": sorted(grupos.values(), key=lambda g: -len(g["solicitudes"]))}
