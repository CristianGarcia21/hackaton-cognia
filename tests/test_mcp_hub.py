"""Tests del hub MCP y del registro de tools para el Voice Agent (issue #9). Sin red."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver import MCPServer

from mcp_servers import ips as servidor_ips
from server import tools_registry as R
from server.mcp_hub import HubMCP
from test_tools_ips import SEDE_SAN_VICENTE, herramientas

PRUEBA = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name("mcp_prueba.py"))])


def correr(coro):
    return asyncio.run(coro)


def servidor_contador() -> tuple[MCPServer, dict]:
    """Servidor en proceso que cuenta cuántas veces se ejecutó cada tool."""
    llamadas = {"n": 0}
    mcp = MCPServer("contador")

    async def sumar(a: int, b: int = 0) -> str:
        """Suma dos números.

        Args:
            a: primer número
            b: segundo número
        """
        llamadas["n"] += 1
        return str(a + b)

    async def lenta() -> str:
        """Tarda 1 s."""
        await asyncio.sleep(1)
        return "tarde"

    async def rota() -> str:
        """Falla siempre."""
        raise RuntimeError("bug")

    for f in (sumar, lenta, rota):
        mcp.add_tool(f)
    return mcp, llamadas


async def con_hub(prueba, *servidores, **kw):
    hub = HubMCP(**kw)
    for nombre, origen, cacheable in servidores:
        hub.registrar(nombre, origen, cacheable=cacheable)
    await hub.iniciar()
    try:
        return await prueba(hub)
    finally:
        await hub.cerrar()


# ------------------------------- hub -------------------------------

def test_hub_lista_las_tools_de_todos_los_servidores():
    h, _ = herramientas()
    mcp, _ = servidor_contador()

    async def prueba(hub):
        assert hub.listo() and hub.estado() == {"ips": True, "contador": True}
        return {t.nombre: t.servidor for t in hub.tools()}

    tools = correr(con_hub(prueba, ("ips", servidor_ips.crear_servidor(h), True), ("contador", mcp, False)))
    assert tools == {"describir_datos": "ips", "buscar_ips": "ips", "contar_capacidad": "ips",
                     "detalle_ips": "ips", "sumar": "contador", "lenta": "contador", "rota": "contador"}


def test_hub_llama_la_tool_y_mide_el_tiempo():
    mcp, _ = servidor_contador()
    r = correr(con_hub(lambda hub: hub.llamar("sumar", {"a": 2, "b": 3}), ("contador", mcp, False)))
    assert (r.texto, r.status, r.servidor, r.cache) == ("5", "ok", "contador", False) and r.ms >= 0


def test_hub_cachea_por_argumentos_solo_si_el_servidor_es_cacheable():
    async def prueba(hub):
        a = await hub.llamar("sumar", {"a": 1, "b": 2})
        b = await hub.llamar("sumar", {"b": 2, "a": 1})  # mismo contenido, otro orden
        c = await hub.llamar("sumar", {"a": 1, "b": 3})
        return a, b, c

    mcp, llamadas = servidor_contador()
    a, b, c = correr(con_hub(prueba, ("contador", mcp, True)))
    assert (a.cache, b.cache, c.cache) == (False, True, False) and llamadas["n"] == 2 and b.texto == "3"

    mcp, llamadas = servidor_contador()
    correr(con_hub(prueba, ("contador", mcp, False)))
    assert llamadas["n"] == 3


def test_hub_no_cachea_errores():
    async def prueba(hub):
        return [await hub.llamar("sumar", {"a": "x"}) for _ in range(2)]

    mcp, _ = servidor_contador()
    a, b = correr(con_hub(prueba, ("contador", mcp, True)))
    assert a.status == b.status == "error" and not b.cache


def test_hub_timeout_devuelve_texto_y_no_cuelga_el_turno():
    mcp, _ = servidor_contador()
    r = correr(con_hub(lambda hub: hub.llamar("lenta", {}), ("contador", mcp, False), timeout_s=0.2))
    assert r.status == "timeout" and r.texto.startswith("Error:") and "tardó" in r.texto and r.ms < 900


def test_hub_tool_desconocida_dice_cuales_hay():
    mcp, _ = servidor_contador()
    r = correr(con_hub(lambda hub: hub.llamar("volar", {}), ("contador", mcp, False)))
    assert r.status == "error" and "volar" in r.texto and "sumar" in r.texto


def test_hub_error_de_validacion_es_corto_y_sin_urls():
    mcp, _ = servidor_contador()
    r = correr(con_hub(lambda hub: hub.llamar("sumar", {"a": "x"}), ("contador", mcp, False)))
    assert r.status == "error" and r.texto.startswith("Error:") and "a" in r.texto
    assert "http" not in r.texto and len(r.texto) < 300


def test_hub_bug_en_la_tool_es_texto():
    mcp, _ = servidor_contador()
    r = correr(con_hub(lambda hub: hub.llamar("rota", {}), ("contador", mcp, False)))
    assert r.status == "error" and r.texto.startswith("Error:")


def test_hub_un_servidor_que_no_arranca_no_tumba_a_los_demas():
    mcp, _ = servidor_contador()
    roto = StdioServerParameters(command=sys.executable, args=["-c", "raise SystemExit(3)"])

    async def prueba(hub):
        assert hub.estado() == {"contador": True, "roto": False} and not hub.listo()
        return await hub.llamar("sumar", {"a": 1})

    r = correr(con_hub(prueba, ("contador", mcp, False), ("roto", roto, False), espera_inicial_s=0.05))
    assert r.texto == "1"


def test_hub_nombres_repetidos_entre_servidores_se_ignoran():
    a, _ = servidor_contador()
    b, _ = servidor_contador()

    async def prueba(hub):
        return [t.servidor for t in hub.tools() if t.nombre == "sumar"]

    assert correr(con_hub(prueba, ("a", a, False), ("b", b, False))) == ["a"]


def test_hub_registrar_dos_veces_el_mismo_nombre_falla():
    hub = HubMCP()
    hub.registrar("x", servidor_contador()[0])
    with pytest.raises(ValueError):
        hub.registrar("x", servidor_contador()[0])


def test_hub_stdio_reinicia_el_subproceso_si_se_cae():
    async def prueba(hub):
        assert (await hub.llamar("eco", {"texto": "a", "veces": 2})).texto == "aa"
        caida = await hub.llamar("morir", {})
        assert caida.status == "error" and "no está disponible" in caida.texto
        for _ in range(100):  # el supervisor relanza el proceso con espera creciente
            if hub.estado()["prueba"]:
                break
            await asyncio.sleep(0.1)
        return await hub.llamar("eco", {"texto": "b"})

    r = correr(con_hub(prueba, ("prueba", PRUEBA, False), espera_inicial_s=0.05, timeout_s=5))
    assert r.texto == "b" and r.status == "ok"


def test_hub_llamadas_concurrentes_no_se_serializan():
    async def prueba(hub):
        return await asyncio.gather(*(hub.llamar("lento", {"segundos": 0.3}) for _ in range(5)))

    async def medir():
        loop = asyncio.get_running_loop()
        t = loop.time()
        rs = await con_hub(prueba, ("prueba", PRUEBA, False), timeout_s=5)
        return rs, loop.time() - t

    rs, _ = correr(medir())
    assert all(r.texto == "listo" for r in rs) and max(r.ms for r in rs) < 1200


# ------------------------------- IPS por MCP -------------------------------

@pytest.mark.parametrize("args", [{"municipio": "Medellín", "naturaleza": "publica"},
                                  {"municipio": "Medellín", "nivel": 2},
                                  {"municipio": "Medellín", "limite": "cinco"},
                                  {"municipio": None, "departamento": "Antioquia"}])
def test_ips_por_mcp_normaliza_igual_que_la_tool_directa(args):
    """El SDK de MCP valida contra la firma ANTES de llamar: la tool debe recibir el valor crudo para
    normalizarlo (si no, "publica" o nivel=2 se rechazan con un error de Pydantic)."""
    h, _ = herramientas(reglas=[("count(*) AS sedes", [{"sedes": "1"}]), ("ORDER BY", [SEDE_SAN_VICENTE])])
    r = correr(con_hub(lambda hub: hub.llamar("buscar_ips", args), ("ips", servidor_ips.crear_servidor(h), True)))
    assert r.status == "ok" and "SAN VICENTE" in r.texto, r.texto


def test_ips_por_mcp_anuncia_el_esquema_del_contrato():
    h, _ = herramientas()

    async def prueba(hub):
        return {t.nombre: t.esquema for t in hub.tools()}

    esquemas = correr(con_hub(prueba, ("ips", servidor_ips.crear_servidor(h), True)))
    props = esquemas["buscar_ips"]["properties"]
    assert props["naturaleza"]["enum"] == ["cualquiera", "Pública", "Privada", "Mixta"]
    assert "Medellín" in props["municipio"]["description"]


def test_ips_sin_catalogo_aun_dice_que_esta_cargando():
    h, datos = herramientas()
    h.catalogo = None
    texto = correr(h.buscar_ips(municipio="Medellín"))
    assert texto.startswith("Error:") and "cargando" in texto and datos.consultas == []


# ------------------------------- registro para el Voice Agent -------------------------------

def _hub_ips(prueba):
    h, _ = herramientas(reglas=[("count(*) AS sedes", [{"sedes": "1"}]), ("ORDER BY", [SEDE_SAN_VICENTE])])
    hub = HubMCP()
    R.registrar_servidores(hub, h)
    return correr(_iniciar_y(hub, prueba))


async def _iniciar_y(hub, prueba):
    await hub.iniciar()
    try:
        return await prueba(hub)
    finally:
        await hub.cerrar()


def test_funciones_agente_tienen_el_formato_de_deepgram():
    async def prueba(hub):
        return R.funciones_agente(hub)

    funciones = _hub_ips(prueba)
    nombres = [f["name"] for f in funciones]
    assert {"describir_datos", "buscar_ips", "contar_capacidad", "detalle_ips"} <= set(nombres)
    for f in funciones:
        assert set(f) == {"name", "description", "parameters"}  # sin endpoint: se ejecutan en el backend
        assert f["parameters"]["type"] == "object"
        assert "title" not in json.dumps(f["parameters"])  # sin ruido que cuesta tokens
    json.dumps(funciones)  # serializable para el mensaje Settings


def test_despachar_acepta_argumentos_como_texto_json_y_quita_nulos():
    async def prueba(hub):
        return await R.despachar(hub, "buscar_ips", '{"municipio": "Medellín", "capacidad": null}')

    r = _hub_ips(prueba)
    assert r.status == "ok" and "SAN VICENTE" in r.texto


@pytest.mark.parametrize("argumentos", ["{no es json", "[1, 2]", '"texto"'])
def test_despachar_argumentos_invalidos_es_texto(argumentos):
    async def prueba(hub):
        return await R.despachar(hub, "buscar_ips", argumentos)

    r = _hub_ips(prueba)
    assert r.status == "error" and r.texto.startswith("Error:") and "argumentos" in r.texto


def test_resumen_es_una_linea_corta():
    assert R.resumen("Encontré 73 sedes en Medellín.\n1. HOSPITAL X") == "Encontré 73 sedes en Medellín."
    assert len(R.resumen("a" * 500)) <= 90
    assert R.resumen("") == ""
