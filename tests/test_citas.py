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
    assert [t.name for t in h.tools()] == ["registrar_solicitud_cita", "listar_solicitudes"]
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
