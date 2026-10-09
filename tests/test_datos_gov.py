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


# ---------------------- Revisión QA de #7 ----------------------

def catalogo_realista() -> D.Catalogo:
    """Subconjunto con valores REALES de la API (verificados): tipos en varios grupos y homónimos."""
    return D.Catalogo(
        total_filas=41427,
        municipios=[("ARMENIA", "Antioquia"), ("ARMENIA", "Quindío"), ("LA PLATA", "Huila"),
                    ("GÓMEZ PLATA", "Antioquia"), ("LA DORADA", "Caldas"), ("LA UNIÓN", "Nariño"),
                    ("LA UNIÓN", "Antioquia"), ("UNIÓN PANAMERICANA", "Chocó"), ("BUCARAMANGA", "Santander"),
                    ("CÚCUTA", "Norte de Santander"), ("CALI", "Cali"), ("RIOHACHA", "La Guajira"),
                    ("POPAYÁN", "Cauca"), ("PALMIRA", "Valle del cauca")],
        capacidades=[("CAMAS", t) for t in ("Adultos", "Atención del Parto", "Cuidado Intensivo Adulto",
                                            "Cuidado Intermedio Pediátrico", "Intensiva Pediátrica",
                                            "Intermedia Pediátrica", "Obstetricia", "Pediátrica", "Psiquiatría",
                                            "Salud Mental")]
        + [("CAMILLAS", "Salud Mental"), ("CAMILLAS", "Observación Pediátrica"), ("SILLAS", "Salud Mental"),
           ("SALAS", "Partos"), ("SALAS", "Sala de Cirugía"), ("SALAS", "Quirófano"),
           ("AMBULANCIAS", "Básica"), ("AMBULANCIAS", "Medicalizada"), ("CONSULTORIOS", "Consulta Externa"),
           ("CONSULTORIOS", "Urgencias")],
    )


def test_cancelar_a_un_solicitante_no_cancela_a_los_demas():
    c = cliente(demora=0.1)

    async def escenario():
        a = asyncio.create_task(c.consultar("SELECT 9"))
        await asyncio.sleep(0.01)
        b = asyncio.create_task(c.consultar("SELECT 9"))
        await asyncio.sleep(0.01)
        a.cancel()
        resultado_b = await b
        with pytest.raises(asyncio.CancelledError):
            await a
        await asyncio.sleep(0.01)
        return resultado_b
    assert correr(escenario()) == [{"q": "SELECT 9"}]
    assert any("SELECT 9" in k for k in c._cache)  # el resultado quedó en caché


def test_el_resultado_es_una_copia_de_la_cache():
    c = cliente()

    async def mutar():
        filas = await c.consultar("SELECT 10")
        filas[0]["q"] = "modificado"
        filas.append({"x": 1})
        return await c.consultar("SELECT 10")
    assert correr(mutar()) == [{"q": "SELECT 10"}]


def _http_que_responde(respuesta: httpx.Response, registro: list):
    async def handler(request):
        registro.append(request)
        return respuesta
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://www.datos.gov.co")


def test_respuesta_no_json_es_fuente_no_disponible():
    registro = []
    c = D.ClienteDatosGov(auth=None, http=_http_que_responde(httpx.Response(200, text="<html>mant</html>"), registro))
    with pytest.raises(D.FuenteNoDisponible, match="no válida"):
        correr(c.consultar("SELECT 11"))


def test_429_no_reintenta():
    registro = []
    c = D.ClienteDatosGov(auth=None, http=_http_que_responde(httpx.Response(429, json={}), registro))
    with pytest.raises(D.FuenteNoDisponible, match="límite de uso"):
        correr(c.consultar("SELECT 12"))
    assert len(registro) == 1


def test_400_es_consulta_invalida():
    registro = []
    c = D.ClienteDatosGov(auth=None, http=_http_que_responde(httpx.Response(400, text="soql error"), registro))
    with pytest.raises(D.ConsultaInvalida):
        correr(c.consultar("SELECT mal"))


def test_presupuesto_total_corta_los_reintentos():
    import time as _t
    c = D.ClienteDatosGov(auth=None, http=api(demora=1), timeout=0.2)
    c.presupuesto = 0.3
    inicio = _t.perf_counter()
    with pytest.raises(D.FuenteNoDisponible):
        correr(c.consultar("SELECT 13"))
    assert _t.perf_counter() - inicio < 0.9


@pytest.mark.parametrize("valor,patron", [("san vicente", "'%SAN VICENTE%'"), ("100%_x", "'%100 X%'"),
                                          ("O'Neill", "'%O''NEILL%'")])
def test_patron_like_quita_comodines_y_escapa(valor, patron):
    assert D.patron_like(valor) == patron


@pytest.mark.parametrize("dicho", ["la", "de", "del", "san", ""])
def test_conectores_sueltos_no_resuelven_departamento(dicho):
    assert catalogo_realista().resolver_departamento(dicho).valores == []


def test_santander_no_incluye_norte_de_santander():
    assert catalogo_realista().resolver_departamento("Santander").valores == ["Santander"]


def test_la_guajira_y_cauca_exactos():
    cat = catalogo_realista()
    assert cat.resolver_departamento("La Guajira").valores == ["La Guajira"]
    assert cat.resolver_departamento("Cauca").valores == ["Cauca"]


def test_cali_como_departamento_es_el_distrito():
    assert catalogo_realista().resolver_departamento("Cali").valores == ["Cali"]


@pytest.mark.parametrize("dicho,esperado", [("La Plata", ["LA PLATA"]), ("La Dorada", ["LA DORADA"])])
def test_articulos_que_son_parte_del_nombre(dicho, esperado):
    r = catalogo_realista().resolver_municipio(dicho)
    assert r.valores == esperado and r.exacto


def test_homonimo_sin_departamento_pide_aclaracion():
    r = catalogo_realista().resolver_municipio("Armenia")
    assert not r.exacto and set(r.sugerencias) == {"ARMENIA (Antioquia)", "ARMENIA (Quindío)"}


def test_homonimo_con_departamento_filtra_y_soql_incluye_el_departamento():
    r = catalogo_realista().resolver_municipio("armenia", "quindio")
    assert r.exacto and r.filtros == [{"municipio": "ARMENIA", "departamento": "Quindío"}]
    assert r.soql() == "(departamento = 'Quindío' AND municipio IN ('ARMENIA'))"


def test_municipio_en_departamento_equivocado_no_adivina():
    r = catalogo_realista().resolver_municipio("Bucaramanga", "Nariño")
    assert r.filtros == [] and not r.exacto and r.sugerencias == ["BUCARAMANGA (Santander)"]


def test_municipio_de_distrito_con_departamento_real():
    r = catalogo_realista().resolver_municipio("Cali", "Valle del Cauca")
    assert r.filtros == [{"municipio": "CALI", "departamento": "Cali"}] and r.exacto


@pytest.mark.parametrize("dicho,pares", [
    ("camas pediátricas", {("CAMAS", "Cuidado Intermedio Pediátrico"), ("CAMAS", "Intensiva Pediátrica"),
                           ("CAMAS", "Intermedia Pediátrica"), ("CAMAS", "Pediátrica")}),
    ("ambulancia medicalizada", {("AMBULANCIAS", "Medicalizada")}),
    ("camas de obstetricia", {("CAMAS", "Obstetricia")}),
    ("camas psiquiátricas", {("CAMAS", "Psiquiatría"), ("CAMAS", "Salud Mental")}),
    ("sillas de salud mental", {("SILLAS", "Salud Mental")}),
    ("partos", {("CAMAS", "Atención del Parto"), ("SALAS", "Partos")}),
    ("sala de partos", {("SALAS", "Partos")}),
    ("cuidado intermedio pediátrico", {("CAMAS", "Cuidado Intermedio Pediátrico"), ("CAMAS", "Intermedia Pediátrica")}),
    ("consulta", {("CONSULTORIOS", "Consulta Externa")}),
])
def test_capacidad_por_pares_grupo_tipo(dicho, pares):
    r = catalogo_realista().resolver_capacidad(dicho)
    assert {(f["nom_grupo_capacidad"], f["nom_descripcion_capacidad"]) for f in r.filtros} == pares


def test_soql_de_capacidad_no_mezcla_grupos():
    r = catalogo_realista().resolver_capacidad("partos")
    assert r.soql() == ("((nom_grupo_capacidad = 'CAMAS' AND nom_descripcion_capacidad IN ('Atención del Parto')) "
                        "OR (nom_grupo_capacidad = 'SALAS' AND nom_descripcion_capacidad IN ('Partos')))")


def test_grupo_solo_filtra_por_grupo():
    r = catalogo_realista().resolver_capacidad("ambulancias")
    assert r.campo == "nom_grupo_capacidad" and r.soql() == "nom_grupo_capacidad IN ('AMBULANCIAS')"


def test_tipo_que_no_existe_en_el_grupo_dicho_pide_aclaracion():
    r = catalogo_realista().resolver_capacidad("camas de urgencias")
    assert r.filtros == [] and not r.exacto and r.sugerencias == ["Urgencias (CONSULTORIOS)"]
