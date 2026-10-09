"""Tests del cliente de datos.gov.co y del resolvedor de búsqueda (issue #7). API simulada: sin red."""

import asyncio
import json

import httpx
import pytest

from server import events as ev
from server.data import datos_gov as D

MUNICIPIOS = [
    {"departamento": "Antioquia", "municipio": "APARTADÓ"}, {"departamento": "Antioquia", "municipio": "MEDELLÍN"},
    {"departamento": "Cali", "municipio": "CALI"}, {"departamento": "Valle del cauca", "municipio": "PALMIRA"},
    {"departamento": "Buenaventura", "municipio": "BUENAVENTURA"}, {"departamento": "Bogotá D.C", "municipio": "BOGOTÁ"},
    {"departamento": "Nariño", "municipio": "PASTO"},
]
CAPACIDADES = [
    {"nom_grupo_capacidad": "CAMAS", "nom_descripcion_capacidad": "Intensiva Adultos"},
    {"nom_grupo_capacidad": "CAMAS", "nom_descripcion_capacidad": "Cuidado Intensivo Adulto"},
    {"nom_grupo_capacidad": "CAMAS", "nom_descripcion_capacidad": "Intensiva Pediátrica"},
    {"nom_grupo_capacidad": "CAMAS", "nom_descripcion_capacidad": "Pediátrica"},
    {"nom_grupo_capacidad": "SALAS", "nom_descripcion_capacidad": "Sala de Cirugía"},
    {"nom_grupo_capacidad": "SALAS", "nom_descripcion_capacidad": "Quirófano"},
    {"nom_grupo_capacidad": "SALAS", "nom_descripcion_capacidad": "Partos"},
    {"nom_grupo_capacidad": "SALAS", "nom_descripcion_capacidad": "Urgencias"},
    {"nom_grupo_capacidad": "AMBULANCIAS", "nom_descripcion_capacidad": "Básica"},
    {"nom_grupo_capacidad": "SILLAS", "nom_descripcion_capacidad": "Sillas de Hemodiálisis"},
]


def api(registro=None, estado_auth=200, fallos_5xx=0, demora=0.0, filas=None):
    """Simula SODA3. Responde según la consulta: count, catálogos o una respuesta genérica."""
    pendientes = {"5xx": fallos_5xx}

    async def handler(request: httpx.Request) -> httpx.Response:
        if registro is not None:
            registro.append(request)
        if demora:
            await asyncio.sleep(demora)
        if pendientes["5xx"] > 0:
            pendientes["5xx"] -= 1
            return httpx.Response(503, json={"error": True})
        if "authorization" in request.headers and estado_auth != 200:
            return httpx.Response(estado_auth, json={"error": True})
        q = json.loads(request.content)["query"]
        if "count(*)" in q and "GROUP BY" not in q:
            return httpx.Response(200, json=[{"n": "41427"}])
        if "GROUP BY departamento, municipio" in q:
            return httpx.Response(200, json=MUNICIPIOS)
        if "GROUP BY nom_grupo_capacidad, nom_descripcion_capacidad" in q:
            return httpx.Response(200, json=CAPACIDADES)
        return httpx.Response(200, json=filas if filas is not None else [{"q": q}])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://www.datos.gov.co")


def cliente(**kw) -> D.ClienteDatosGov:
    registro = kw.pop("registro", None)
    return D.ClienteDatosGov(auth=kw.pop("auth", ("id", "secreto")), http=api(registro=registro, **kw),
                             timeout=kw.pop("timeout", 3))


def correr(coro):
    return asyncio.run(coro)


# ------------------------------- SoQL seguro -------------------------------

def test_texto_soql_escapa_comillas():
    assert D.texto("O'Neill") == "'O''Neill'"


def test_en_arma_un_in_escapado():
    assert D.en("departamento", ["Cali", "Valle del cauca"]) == "departamento IN ('Cali', 'Valle del cauca')"


# ------------------------------- consultar -------------------------------

def test_consultar_usa_soda3_post_con_auth_basic():
    registro = []
    c = cliente(registro=registro)
    filas = correr(c.consultar("SELECT nom_sede_ips LIMIT 3"))
    assert filas == [{"q": "SELECT nom_sede_ips LIMIT 3"}]
    r = registro[0]
    assert r.method == "POST" and r.url.path == "/api/v3/views/s2ru-bqt6/query.json"
    assert r.headers["authorization"].startswith("Basic ")
    assert json.loads(r.content)["includeSynthetic"] is False


def test_cache_evita_repetir_la_consulta():
    registro = []
    c = cliente(registro=registro)

    async def dos_veces():
        await c.consultar("SELECT 1")
        await c.consultar("SELECT 1")
    correr(dos_veces())
    assert len(registro) == 1 and c.metricas["aciertos"] == 1 and c.metricas["fallos"] == 1


def test_consultas_iguales_simultaneas_hacen_una_sola_peticion():
    registro = []
    c = cliente(registro=registro, demora=0.05)

    async def a_la_vez():
        return await asyncio.gather(*(c.consultar("SELECT 2") for _ in range(5)))
    resultados = correr(a_la_vez())
    assert len(registro) == 1 and all(r == resultados[0] for r in resultados)


def test_cache_expira_por_ttl(monkeypatch):
    registro = []
    c = cliente(registro=registro)
    reloj = [1000.0]
    monkeypatch.setattr(D.time, "monotonic", lambda: reloj[0])

    async def con_espera():
        await c.consultar("SELECT 3")
        reloj[0] += c.ttl + 1
        await c.consultar("SELECT 3")
    correr(con_espera())
    assert len(registro) == 2


def test_cache_tiene_limite_de_tamano():
    c = cliente()
    c.max_cache = 3

    async def muchas():
        for i in range(5):
            await c.consultar(f"SELECT {i}")
    correr(muchas())
    assert len(c._cache) == 3


def test_reintenta_una_vez_ante_5xx():
    registro = []
    c = cliente(registro=registro, fallos_5xx=1)
    assert correr(c.consultar("SELECT 4"))
    assert len(registro) == 2


def test_si_la_key_es_rechazada_reintenta_sin_auth():
    registro = []
    c = cliente(registro=registro, estado_auth=403)
    assert correr(c.consultar("SELECT 5")) == [{"q": "SELECT 5"}]
    assert "authorization" not in registro[-1].headers
    assert c.auth is None  # no vuelve a intentar con una key inválida


def test_api_caida_lanza_fuente_no_disponible():
    c = cliente(fallos_5xx=10)
    with pytest.raises(D.FuenteNoDisponible, match="datos.gov.co"):
        correr(c.consultar("SELECT 6"))


def test_timeout_lanza_fuente_no_disponible():
    c = D.ClienteDatosGov(auth=None, http=api(demora=1), timeout=0.05)
    with pytest.raises(D.FuenteNoDisponible):
        correr(c.consultar("SELECT 7"))


def test_errores_no_se_guardan_en_cache():
    registro = []
    c = cliente(registro=registro, fallos_5xx=2)

    async def dos():
        with pytest.raises(D.FuenteNoDisponible):
            await c.consultar("SELECT 8")
        return await c.consultar("SELECT 8")
    assert correr(dos())
    assert len(registro) == 3


# ------------------------------- catálogos -------------------------------

def cargar_catalogo():
    estados = []
    cat = correr(D.cargar_catalogo(cliente(), on_status=estados.append))
    return cat, estados


def test_catalogo_emite_source_status():
    cat, estados = cargar_catalogo()
    assert [e.status for e in estados] == ["conectando", "listo"]
    assert all(isinstance(e, ev.SourceStatus) for e in estados)
    assert estados[-1].rows == 41427 and estados[-1].progress == 1
    assert cat.total_filas == 41427


def test_catalogo_stats():
    cat, _ = cargar_catalogo()
    s = cat.stats()
    assert s["registros"] == 41427 and s["municipios"] == 7 and s["tipos_capacidad"] == 10
    assert s["departamentos"] == 4  # Antioquia, Valle del Cauca (con Cali y Buenaventura), Bogotá, Nariño


# ------------------------------- resolvedor -------------------------------

@pytest.mark.parametrize("dicho,valores", [
    ("Valle del Cauca", {"Cali", "Valle del cauca", "Buenaventura"}),
    ("el valle", {"Cali", "Valle del cauca", "Buenaventura"}),
    ("antioquia", {"Antioquia"}), ("Bogota", {"Bogotá D.C"}), ("narino", {"Nariño"}),
])
def test_resolver_departamento(dicho, valores):
    cat, _ = cargar_catalogo()
    r = cat.resolver_departamento(dicho)
    assert set(r.valores) == valores and r.campo == "departamento"


@pytest.mark.parametrize("dicho,valores", [
    ("Apartado", {"APARTADÓ"}), ("medellin", {"MEDELLÍN"}), ("Medeyin", {"MEDELLÍN"}), ("cali", {"CALI"})])
def test_resolver_municipio_sin_tildes_y_con_errores(dicho, valores):
    cat, _ = cargar_catalogo()
    assert set(cat.resolver_municipio(dicho).valores) == valores


def test_resolver_municipio_desconocido_sugiere():
    cat, _ = cargar_catalogo()
    r = cat.resolver_municipio("Narnia")
    assert r.valores == [] and r.sugerencias


@pytest.mark.parametrize("dicho,campo,valores", [
    ("UCI", "nom_descripcion_capacidad", {"Intensiva Adultos", "Cuidado Intensivo Adulto", "Intensiva Pediátrica"}),
    ("cirugía", "nom_descripcion_capacidad", {"Sala de Cirugía", "Quirófano"}),
    ("quirofanos", "nom_descripcion_capacidad", {"Sala de Cirugía", "Quirófano"}),
    ("ambulancias", "nom_grupo_capacidad", {"AMBULANCIAS"}),
    ("camas", "nom_grupo_capacidad", {"CAMAS"}),
    ("hemodialisis", "nom_descripcion_capacidad", {"Sillas de Hemodiálisis"}),
    ("pediatria", "nom_descripcion_capacidad", {"Intensiva Pediátrica", "Pediátrica"}),
    ("urgencias", "nom_descripcion_capacidad", {"Urgencias"}),
])
def test_resolver_capacidad_con_sinonimos(dicho, campo, valores):
    cat, _ = cargar_catalogo()
    r = cat.resolver_capacidad(dicho)
    assert r.campo == campo and set(r.valores) == valores


def test_resolucion_a_soql():
    cat, _ = cargar_catalogo()
    assert cat.resolver_departamento("valle").soql() == \
        "departamento IN ('Buenaventura', 'Cali', 'Valle del cauca')"


# ------------------------------- integración real (opcional) -------------------------------

@pytest.mark.skipif("not config.getoption('--red')", reason="usa la API real: pytest --red")
def test_api_real():
    from server import config

    async def real():
        c = D.ClienteDatosGov(auth=config.DATOS_GOV_AUTH)
        try:
            cat = await D.cargar_catalogo(c)
            valle = cat.resolver_departamento("Valle del Cauca")
            uci = cat.resolver_capacidad("UCI")
            filas = await c.consultar(f"SELECT sum(num_cantidad_capacidad_instalada) AS n "
                                      f"WHERE {valle.soql()} AND {uci.soql()}")
            return cat, filas
        finally:
            await c.aclose()
    cat, filas = asyncio.run(real())
    assert cat.total_filas > 40000 and cat.stats()["departamentos"] == 33
    assert int(filas[0]["n"]) > 0


@pytest.mark.parametrize("dicho,valores", [
    ("camas de UCI pediátrica", {"Intensiva Pediátrica"}),
    ("uci para adultos", {"Intensiva Adultos", "Cuidado Intensivo Adulto"}),
    ("UCI", {"Intensiva Adultos", "Cuidado Intensivo Adulto", "Intensiva Pediátrica"}),
])
def test_modificadores_refinan_la_capacidad(dicho, valores):
    cat, _ = cargar_catalogo()
    assert set(cat.resolver_capacidad(dicho).valores) == valores
