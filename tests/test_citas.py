"""Tests de las solicitudes de cita (issue #16): SQLite en una carpeta temporal y MCP en memoria. Sin red."""

import asyncio
import sqlite3

import pytest

from mcp_servers import citas as servidor_citas
from server import tools_registry as R
from server.mcp_hub import HubMCP
from server.tools import citas as C
from test_mcp_hub import con_hub

MARIA = {"paciente": "María Gómez", "sede_codigo": "500102126-01", "motivo": "valoración para cirugía",
         "fecha_preferida": "el lunes en la mañana", "sede_nombre": "Hospital Pablo Tobón Uribe"}


def correr(coro):
    return asyncio.run(coro)


@pytest.fixture
def citas(tmp_path):
    return C.HerramientasCitas(tmp_path / "sub" / "citas.db")


def test_registrar_devuelve_id_y_estado_honesto(citas):
    texto = correr(citas.registrar_solicitud_cita(**MARIA))
    assert texto.startswith("Solicitud #1 registrada") and C.ESTADO_PENDIENTE in texto
    assert "NO es una cita confirmada" in texto
    fila = citas.solicitud(1)
    assert fila["estado"] == C.ESTADO_PENDIENTE and fila["sede_codigo"] == "500102126-01"
    assert fila["paciente"] == "María Gómez" and fila["telefono"] is None


def test_la_base_se_crea_en_el_primer_uso_y_no_al_instanciar(tmp_path):
    ruta = tmp_path / "nueva" / "citas.db"
    h = C.HerramientasCitas(ruta)
    assert [t.name for t in h.tools()] == ["proponer_cita", "registrar_solicitud_cita", "listar_solicitudes",
                                          "horarios_ocupados"]
    assert not ruta.exists()
    correr(h.listar_solicitudes())
    assert ruta.exists()


def test_la_misma_solicitud_repetida_no_se_duplica(citas):
    correr(citas.registrar_solicitud_cita(**MARIA))
    texto = correr(citas.registrar_solicitud_cita(**MARIA))
    assert "ya estaba registrada" in texto and "#1" in texto
    assert len(citas.solicitudes()) == 1
    otra = correr(citas.registrar_solicitud_cita(**(MARIA | {"motivo": "control"})))
    assert otra.startswith("Solicitud #2 registrada")


@pytest.mark.parametrize("cambios, esperado", [
    ({"paciente": "  "}, "falta el nombre del paciente"),
    ({"motivo": ""}, "falta el motivo"),
    ({"sede_codigo": "Hospital X"}, "no es un id de sede válido"),
    ({"sede_codigo": "123"}, "no lo inventes"),
    ({"telefono": "12"}, "no parece válido"),
])
def test_entradas_invalidas_son_texto_y_no_se_guardan(citas, cambios, esperado):
    texto = correr(citas.registrar_solicitud_cita(**(MARIA | cambios)))
    assert texto.startswith("Error:") and esperado in texto
    assert citas.solicitudes() == []


def test_id_de_sede_con_espacios_se_acepta(citas):
    texto = correr(citas.registrar_solicitud_cita(**(MARIA | {"sede_codigo": " 500102126 - 01 "})))
    assert texto.startswith("Solicitud #1") and citas.solicitud(1)["sede_codigo"] == "500102126-01"


def test_listar_filtra_por_paciente_sin_tildes_ni_mayusculas(citas):
    correr(citas.registrar_solicitud_cita(**MARIA))
    correr(citas.registrar_solicitud_cita(**(MARIA | {"paciente": "Juan Pérez", "motivo": "control"})))
    texto = correr(citas.listar_solicitudes(paciente="MARIA"))
    assert texto.startswith("1 solicitud de 'MARIA'") and "María Gómez" in texto and "Juan" not in texto
    assert "2 solicitudes" in correr(citas.listar_solicitudes())
    assert "No hay solicitudes registradas para 'Pedro'" in correr(citas.listar_solicitudes("Pedro"))


def test_listar_sin_registros(citas):
    assert correr(citas.listar_solicitudes()) == "Todavía no hay solicitudes de cita registradas."


def test_texto_del_usuario_no_inyecta_sql(citas):
    correr(citas.registrar_solicitud_cita(**(MARIA | {"paciente": "Ana'); DROP TABLE solicitudes;--"})))
    assert len(citas.solicitudes()) == 1
    assert "No hay solicitudes" in correr(citas.listar_solicitudes("%"))  # el % del usuario no es comodín


def test_fallo_de_sqlite_es_texto(citas, monkeypatch):
    def roto(*_):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(citas, "_insertar", roto)
    texto = correr(citas.registrar_solicitud_cita(**MARIA))
    assert texto.startswith("Error:") and "Intenta de nuevo" in texto


# ------------------------------- por MCP (en memoria, a través del hub) -------------------------------

def test_por_mcp_registra_y_lista(citas):
    async def prueba(hub):
        r1 = await hub.llamar("registrar_solicitud_cita", MARIA)
        r2 = await hub.llamar("listar_solicitudes", {"paciente": "maría"})
        return r1, r2

    r1, r2 = correr(con_hub(prueba, ("citas", servidor_citas.crear_servidor(citas), False)))
    assert r1.status == "ok" and "Solicitud #1 registrada" in r1.texto, r1.texto
    assert r2.status == "ok" and "María Gómez" in r2.texto


def test_por_mcp_los_errores_llegan_como_texto_en_espanol(citas):
    async def prueba(hub):
        return await hub.llamar("registrar_solicitud_cita", {"paciente": "Ana", "sede_codigo": "x", "motivo": "y"})

    r = correr(con_hub(prueba, ("citas", servidor_citas.crear_servidor(citas), False)))
    assert "no es un id de sede válido" in r.texto


def test_por_mcp_anuncia_el_esquema_del_contrato(citas):
    async def prueba(hub):
        return {t.nombre: t.esquema for t in hub.tools()}

    esquemas = correr(con_hub(prueba, ("citas", servidor_citas.crear_servidor(citas), False)))
    props = esquemas["registrar_solicitud_cita"]["properties"]
    assert set(esquemas["registrar_solicitud_cita"]["required"]) == {"paciente", "sede_codigo", "motivo"}
    assert "500102126-01" in props["sede_codigo"]["description"]


def test_el_registro_del_agente_incluye_citas_sin_cache(citas):
    from test_tools_ips import herramientas

    h, _ = herramientas()
    hub = HubMCP()
    R.registrar_servidores(hub, h, citas)

    async def prueba():
        await hub.iniciar()
        try:
            nombres = {f["name"] for f in R.funciones_agente(hub)}
            a = await hub.llamar("registrar_solicitud_cita", MARIA)
            b = await hub.llamar("registrar_solicitud_cita", MARIA)  # si se cacheara, b vendría de la caché
            return nombres, a, b
        finally:
            await hub.cerrar()

    nombres, a, b = correr(prueba())
    assert {"registrar_solicitud_cita", "listar_solicitudes", "buscar_ips"} <= nombres
    assert not a.cache and not b.cache and "ya estaba registrada" in b.texto


# ------------------------------- agenda por franjas y /citas -------------------------------

from datetime import date, datetime, timedelta  # noqa: E402


def manana_a(hora: str) -> str:
    d = date.today() + timedelta(days=1)
    if d.weekday() == 6:
        d += timedelta(days=1)
    return f"{d.isoformat()}T{hora}"


SEDE = {"sede_codigo": "7600102870-01", "sede_nombre": "FUNDACION VALLE DEL LILI", "motivo": "cirugía"}


def test_la_misma_franja_en_la_misma_sede_esta_ocupada_y_ofrece_libres(citas):
    assert "registrada" in correr(citas.registrar_solicitud_cita(paciente="Ana Ruiz", fecha_hora=manana_a("10:00"), **SEDE))
    texto = correr(citas.registrar_solicitud_cita(paciente="Luis Pérez", fecha_hora=manana_a("10:10"), **SEDE))
    assert texto.startswith("Error:") and "10:00" in texto and "ya tiene una solicitud" in texto
    assert "09:30" in texto and "10:30" in texto  # las libres más cercanas
    otra_sede = correr(citas.registrar_solicitud_cita(paciente="Luis Pérez", fecha_hora=manana_a("10:00"),
                                                      sede_codigo="500102104-01", motivo="consulta"))
    assert "registrada" in otra_sede  # misma hora en otra sede: sí se puede


@pytest.mark.parametrize("fecha_hora,mensaje", [("ayer a las 10", "no entend"), ("2020-01-01T10:00", "ya pasó"),
                                                ("__manana__T05:00", "entre las 7:00")])
def test_fecha_hora_invalida_explica_como_corregir(citas, fecha_hora, mensaje):
    fecha_hora = fecha_hora.replace("__manana__", manana_a("10:00")[:10])
    texto = correr(citas.registrar_solicitud_cita(paciente="Ana Ruiz", fecha_hora=fecha_hora, **SEDE))
    assert texto.startswith("Error:") and mensaje in texto


def test_horarios_ocupados_lista_tomadas_y_libres(citas):
    correr(citas.registrar_solicitud_cita(paciente="Ana Ruiz", fecha_hora=manana_a("08:00"), **SEDE))
    texto = correr(citas.horarios_ocupados(sede_codigo=SEDE["sede_codigo"], fecha=manana_a("08:00")[:10]))
    assert "ocupadas 08:00" in texto and "07:00" in texto and "no la agenda real" in texto


def test_vista_publica_agrupa_por_sede_y_enmascara(citas):
    correr(citas.registrar_solicitud_cita(paciente="María Gómez Ruiz", documento="1234567", telefono="3001234567",
                                          fecha_hora=manana_a("10:00"), **SEDE))
    v = C.vista_publica(citas.solicitudes())
    s = v["grupos"][0]["solicitudes"][0]
    assert v["total"] == 1 and v["grupos"][0]["sede_nombre"] == "FUNDACION VALLE DEL LILI"
    assert s["paciente"] == "María G." and "1234567" not in str(v) and "3001234567" not in str(v)
    assert s["google"].startswith("https://calendar.google.com/calendar/render?action=TEMPLATE")
    assert s["ics"] == f"/api/citas/{s['id']}.ics" and "10:00" in s["cuando"]


def test_ics_valido_con_estado_pendiente(citas):
    correr(citas.registrar_solicitud_cita(paciente="Ana Ruiz", fecha_hora=manana_a("10:00"), **SEDE))
    texto = C.ics(citas.solicitudes()[0])
    assert texto.startswith("BEGIN:VCALENDAR") and "DTSTART;TZID=America/Bogota:" in texto
    assert "pendiente de confirmación por la IPS" in texto and texto.endswith("END:VCALENDAR\r\n")


def test_sin_fecha_hora_no_hay_calendario(citas):
    correr(citas.registrar_solicitud_cita(paciente="Ana Ruiz", fecha_preferida="el lunes", **SEDE))
    s = citas.solicitudes()[0]
    assert C.ics(s) is None and C.enlace_google(s) is None


def test_semillas_solo_si_esta_vacia(citas):
    assert citas.sembrar() == 3 and len(citas.solicitudes()) == 3
    assert citas.sembrar() == 0  # ya hay datos: no se repiten
    assert all("Paciente Demo" in s["paciente"] for s in citas.solicitudes())


def test_base_vieja_sin_columna_fecha_hora_se_migra(tmp_path):
    import sqlite3
    ruta = tmp_path / "vieja.db"
    con = sqlite3.connect(ruta)
    con.execute(C.ESQUEMA.replace(",\n    fecha_hora TEXT", ""))
    con.commit()
    con.close()
    h = C.HerramientasCitas(ruta)
    assert "registrada" in correr(h.registrar_solicitud_cita(paciente="Ana", fecha_hora=manana_a("10:00"), **SEDE))
