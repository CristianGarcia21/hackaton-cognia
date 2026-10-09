"""Ficha visual de la cita (evento `action`, kind "cita"/"evento") a partir de las tools del flujo de agendamiento.

    proponer_cita        → fase "por_confirmar"  (la ficha aparece mientras el agente pide el "sí")
    registrar_solicitud  → fase "registrada"     (ya está en /citas, lista para enviar a la IPS)
    crear_evento_cita    → fase "en_calendario"  (con enlace a Google Calendar si lo hay, y .ics)

La UI (web/tarjeta-cita.js) actualiza una sola ficha de fase en fase. El nombre va abreviado: la pantalla
puede estar proyectada.
"""

import re

from server import events as ev
from server.tools import citas as C

_NUMERO = re.compile(r"#(\d+)")
_ENLACE = re.compile(r"https://\S+")
DIAS_CORTOS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
MESES_CORTOS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _base(args: dict) -> dict:
    datos: dict = {"sede": args.get("sede_nombre") or "", "paciente": C._enmascarar(args.get("paciente") or ""),
                   "motivo": args.get("motivo") or ""}
    try:
        f = C._franja(args.get("fecha_hora") or "")
    except C.EntradaInvalida:
        return datos
    return datos | {"inicio": f.isoformat(timespec="minutes"), "dia": f.day, "mes": MESES_CORTOS[f.month - 1],
                    "semana": DIAS_CORTOS[f.weekday()], "hora": C._hora(f), "fecha": C._fecha(f)}


def tarjeta(nombre: str, args: dict, texto: str) -> ev.Action | None:
    """Evento para la ficha, o None si la tool no es del flujo de citas o falló."""
    if texto.startswith("Error:"):
        return None
    if nombre == "proponer_cita":
        return ev.Action(kind="cita", data=_base(args) | {"fase": "por_confirmar", "estado": "Por confirmar"})
    if nombre == "registrar_solicitud_cita":
        m = _NUMERO.search(texto)
        sid = int(m[1]) if m else None
        return ev.Action(kind="cita", data=_base(args) | {"fase": "registrada", "solicitud_id": sid,
                                                          "estado": C.ESTADO_PENDIENTE,
                                                          "ics": f"/api/citas/{sid}.ics" if sid else None},
                         link="/citas")
    if nombre == "crear_evento_cita":
        m = _NUMERO.search(texto)
        g = _ENLACE.search(texto)
        sid = int(m[1]) if m else None
        return ev.Action(kind="evento", data={"fase": "en_calendario", "solicitud_id": sid,
                                              "google": g[0].rstrip(".,)") if g else None,
                                              "ics": f"/api/citas/{sid}.ics" if sid else None,
                                              "calendario": "Google Calendar" if g else "Calendario de Kognia"},
                         link="/citas")
    if nombre == "exportar_solicitudes_excel":
        m = re.search(r"(\d+) solicitud", texto)
        return ev.Action(kind="excel", data={"filas": int(m[1]) if m else None, "resumen": texto.split(".")[0]},
                         link="/api/citas/excel")
    return None
