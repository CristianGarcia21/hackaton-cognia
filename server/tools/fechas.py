"""Fechas dichas en voz → datetime, de forma DETERMINISTA (el LLM de voz calcula mal los días: convirtió
"lunes 12" en "lunes 16"). Lo usan las tools de citas: el agente pasa lo que dijo el usuario tal cual.

    interpretar("el lunes 12 a las 10 y media")       -> datetime(…, 12, 10, 30)
    interpretar("mañana a las 3 de la tarde")          -> mañana 15:00
    interpretar_dia("el viernes")                      -> date del próximo viernes
"""

import re
import unicodedata
from datetime import date, datetime, timedelta

DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]
NUMEROS = {"una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
           "nueve": 9, "diez": 10, "once": 11, "doce": 12}


class FechaNoEntendida(ValueError):
    pass


def _limpio(texto: str) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()
    for palabra, n in NUMEROS.items():
        t = re.sub(rf"\b{palabra}\b", str(n), t)
    return " ".join(t.split())


def interpretar_dia(texto: str, hoy: date | None = None) -> date:
    hoy = hoy or date.today()
    t = _limpio(texto)
    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})", t):
        return date(int(m[1]), int(m[2]), int(m[3]))
    t = t.replace("de la manana", "").replace("por la manana", "")
    if "pasado manana" in t:
        return hoy + timedelta(days=2)
    if re.search(r"\bmanana\b", t):
        return hoy + timedelta(days=1)
    if re.search(r"\bhoy\b", t):
        return hoy
    sin_hora = re.sub(r"\b(a la|a las|las)\s+\d{1,2}(:\d{2})?", " ", t)
    mes = next((i + 1 for i, nombre in enumerate(MESES) if re.search(rf"\b{nombre}\b", sin_hora)), None)
    if m := re.search(r"\b(\d{1,2})\b(?!:)", sin_hora):
        dia = int(m[1])
        if 1 <= dia <= 31:
            mes_ = mes or hoy.month
            anio = hoy.year
            if mes is None and dia < hoy.day:  # "el 3" ya pasó este mes: es el próximo
                mes_, anio = (1, anio + 1) if mes_ == 12 else (mes_ + 1, anio)
            elif mes is not None and (mes, dia) < (hoy.month, hoy.day):
                anio += 1
            try:
                return date(anio, mes_, dia)
            except ValueError:
                raise FechaNoEntendida(f"el día {dia} no existe en ese mes") from None
    for i, nombre in enumerate(DIAS):
        if re.search(rf"\b{nombre}\b", t):
            return hoy + timedelta(days=(i - hoy.weekday()) % 7 or 7) if i != hoy.weekday() else hoy
    raise FechaNoEntendida(f"no entendí qué día es '{texto}'")


def interpretar_hora(texto: str) -> tuple[int, int]:
    t = _limpio(texto)
    m = (re.search(r"\b(\d{1,2}):(\d{2})\b", t)
         or re.search(r"\ba las? (\d{1,2})(?: y (media|cuarto|\d{1,2}))?", t)
         or re.search(r"\b(\d{1,2})(?: y (media|cuarto))? (?:de la|en la|por la) (?:manana|tarde|noche)", t))
    if not m:
        raise FechaNoEntendida(f"no entendí la hora en '{texto}'")
    hora = int(m[1])
    resto = m[2] if m.lastindex and m.lastindex >= 2 else None
    minutos = 30 if resto == "media" else 15 if resto == "cuarto" else int(resto) if resto and resto.isdigit() else 0
    if re.search(r"\b(tarde|noche)\b", t) and hora < 12:
        hora += 12
    elif not re.search(r"\bmanana\b", t) and 1 <= hora <= 6:  # "a las 3" en una agenda de día es de la tarde
        hora += 12
    if not (0 <= hora <= 23 and 0 <= minutos <= 59):
        raise FechaNoEntendida(f"la hora de '{texto}' no es válida")
    return hora, minutos


def interpretar(texto: str, hoy: date | None = None) -> datetime:
    """Día y hora juntos. Acepta ISO (AAAA-MM-DDTHH:MM) o lenguaje natural en español."""
    t = str(texto or "").strip()
    try:
        return datetime.fromisoformat(t.replace(" ", "T"))
    except ValueError:
        pass
    dia = interpretar_dia(t, hoy)
    hora, minutos = interpretar_hora(t)
    return datetime(dia.year, dia.month, dia.day, hora, minutos)
