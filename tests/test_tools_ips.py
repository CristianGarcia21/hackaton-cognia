"""Tests de las tools de IPS (issue #8). Cliente de datos.gov.co simulado: sin red."""

import asyncio

import pytest

from server.data import datos_gov as D
from server.tools import ips as T

CATALOGO = D.Catalogo(
    total_filas=41427,
    municipios=[("MEDELLÍN", "Antioquia"), ("ARMENIA", "Antioquia"), ("ARMENIA", "Quindío"), ("CALI", "Cali"),
                ("PALMIRA", "Valle del cauca"), ("BUENAVENTURA", "Buenaventura"), ("BOGOTÁ", "Bogotá D.C")],
    capacidades=[("CAMAS", "Cuidado Intensivo Adulto"), ("CAMAS", "Intensiva Adultos"), ("CAMAS", "Pediátrica"),
                 ("SALAS", "Sala de Cirugía"), ("SALAS", "Quirófano"), ("SALAS", "Partos"),
                 ("CAMAS", "Atención del Parto"), ("AMBULANCIAS", "Básica")],
)

SEDE_LAS_AMERICAS = {"departamento": "Antioquia", "municipio": "MEDELLÍN", "c_digo_sede": "500102126",
                     "n_mero_sede": "01", "nom_sede_ips": "CLÍNICA LAS AMERICAS",
                     "nombre_prestador": "PROMOTORA MEDICA LAS AMERICAS S.A", "naturaleza": "Privada",
                     "direcci_n": "DIAGONAL 75B N° 2A-80", "tel_fono": "3421010", "cantidad": "28"}
SEDE_SAN_VICENTE = {"departamento": "Antioquia", "municipio": "MEDELLÍN", "c_digo_sede": "500100011",
                    "n_mero_sede": "02", "nom_sede_ips": "FUNDACION HOSPITALARIA SAN VICENTE DE PAUL",
                    "nombre_prestador": "FUNDACION HOSPITALARIA SAN VICENTE DE PAUL", "naturaleza": "Privada",
                    "num_nivel_atencion": "3", "direcci_n": "CL 64 # 51D-154", "tel_fono": "4441333",
                    "cantidad": "21"}


class DatosFalsos:
    """Imita ClienteDatosGov.consultar: responde según fragmentos del SoQL y registra cada consulta."""

    def __init__(self, reglas: list[tuple[str, list[dict]]] | None = None, falla: bool = False):
        self.reglas, self.falla, self.consultas = reglas or [], falla, []

    async def consultar(self, soql: str, limite: int = 1000) -> list[dict]:
        self.consultas.append(soql)
        if self.falla:
            raise D.FuenteNoDisponible("datos.gov.co no responde (HTTP 503)")
        for fragmento, filas in self.reglas:
            if fragmento in soql:
                return filas
        return []


def herramientas(**kw) -> tuple[T.HerramientasIPS, DatosFalsos]:
    datos = DatosFalsos(**kw)
    return T.HerramientasIPS(datos, CATALOGO), datos


def correr(coro):
    return asyncio.run(coro)


# ------------------------------- buscar_ips -------------------------------

def test_buscar_ips_arma_soql_deduplicado_con_filtros_exactos():
    h, datos = herramientas(reglas=[("count(*) AS sedes", [{"sedes": "73"}]),
                                    ("ORDER BY cantidad DESC", [SEDE_LAS_AMERICAS, SEDE_SAN_VICENTE])])
    texto = correr(h.buscar_ips(municipio="Medellín", capacidad="cirugía"))
    principal = next(q for q in datos.consultas if "ORDER BY cantidad DESC" in q)
    assert "|>" in principal and "sum(num_cantidad_capacidad_instalada) AS cantidad" in principal
    assert "(departamento = 'Antioquia' AND municipio IN ('MEDELLÍN'))" in principal
    assert "nom_descripcion_capacidad IN ('Quirófano', 'Sala de Cirugía')" in principal
    assert "LIMIT 5" in principal
    assert "73 sedes" in texto and "CLÍNICA LAS AMERICAS" in texto and "SAN VICENTE DE PAUL" in texto
    assert "500102126-01" in texto  # id de sede para agendar
    assert "nivel sin dato" in texto and "nivel 3" in texto
    assert "datos.gov.co" in texto and "no incluye horarios" in texto.lower()


def test_buscar_ips_homonimo_pide_aclaracion_sin_consultar():
    h, datos = herramientas()
    texto = correr(h.buscar_ips(municipio="Armenia"))
    assert datos.consultas == []
    assert "ARMENIA (Antioquia)" in texto and "ARMENIA (Quindío)" in texto


def test_buscar_ips_capacidad_desconocida_sugiere_sin_consultar():
    h, datos = herramientas()
    texto = correr(h.buscar_ips(municipio="Medellín", capacidad="rayos x"))
    assert datos.consultas == [] and "rayos x" in texto.lower()


def test_buscar_ips_avisa_cuando_interpreta_un_nombre():
    h, _ = herramientas(reglas=[("count(*) AS sedes", [{"sedes": "1"}]), ("ORDER BY", [SEDE_LAS_AMERICAS])])
    texto = correr(h.buscar_ips(municipio="Medeyin", capacidad="cirugía"))
    assert "Interpreté" in texto and "MEDELLÍN" in texto


def test_buscar_ips_sin_resultados_lo_dice():
    h, _ = herramientas(reglas=[("count(*) AS sedes", [{"sedes": "0"}])])
    texto = correr(h.buscar_ips(municipio="Medellín", capacidad="cirugía"))
    assert "No encontré" in texto


def test_buscar_ips_fuente_caida_es_error_honesto():
    h, _ = herramientas(falla=True)
    assert correr(h.buscar_ips(municipio="Medellín")).startswith("Error: datos.gov.co no responde")


def test_buscar_ips_filtra_naturaleza_y_nivel():
    h, datos = herramientas(reglas=[("count(*) AS sedes", [{"sedes": "2"}]), ("ORDER BY", [SEDE_SAN_VICENTE])])
    correr(h.buscar_ips(departamento="Antioquia", naturaleza="Pública", nivel="sin dato"))
    q = datos.consultas[0]
    assert "naturaleza = 'Pública'" in q and "num_nivel_atencion IS NULL" in q




# ------------------------------- contar_capacidad -------------------------------

def test_contar_por_departamento_une_los_distritos_y_separa_unidades():
    filas = [{"departamento": "Bogotá D.C", "nom_grupo_capacidad": "CAMAS", "cantidad": "1779", "sedes": "60"},
             {"departamento": "Cali", "nom_grupo_capacidad": "CAMAS", "cantidad": "871", "sedes": "30"},
             {"departamento": "Valle del cauca", "nom_grupo_capacidad": "CAMAS", "cantidad": "200", "sedes": "10"},
             {"departamento": "Buenaventura", "nom_grupo_capacidad": "CAMAS", "cantidad": "32", "sedes": "2"}]
    h, datos = herramientas(reglas=[("|> SELECT nom_grupo_capacidad, sum(cantidad)",
                                     [{"nom_grupo_capacidad": "CAMAS", "cantidad": "2882", "sedes": "102"}]),
                                    ("GROUP BY departamento, nom_grupo_capacidad", filas)])
    texto = correr(h.contar_capacidad(agrupar_por="departamento", capacidad="UCI"))
    assert all(q.count("|>") == 2 for q in datos.consultas)  # deduplicar → sumar por sede → agrupar
    assert "Valle del Cauca: 1.103 camas en 42 sedes" in texto
    assert "Bogotá D.C: 1.779 camas en 60 sedes" in texto
    assert texto.index("Bogotá") < texto.index("Valle")  # ordenado de mayor a menor
    assert "Total: 2.882 camas en 102 sedes" in texto


def test_contar_mezcla_de_grupos_reporta_cada_unidad():
    filas = [{"nom_grupo_capacidad": "CAMAS", "cantidad": "31", "sedes": "9"},
             {"nom_grupo_capacidad": "SALAS", "cantidad": "17", "sedes": "12"}]
    h, _ = herramientas(reglas=[("GROUP BY nom_grupo_capacidad", filas)])
    texto = correr(h.contar_capacidad(capacidad="partos", municipio="Medellín"))
    assert "31 camas" in texto and "17 salas" in texto


def test_contar_por_nivel_muestra_sin_dato():
    filas = [{"num_nivel_atencion": "3", "nom_grupo_capacidad": "CAMAS", "cantidad": "50", "sedes": "5"},
             {"nom_grupo_capacidad": "CAMAS", "cantidad": "80", "sedes": "9"}]
    h, _ = herramientas(reglas=[("|> SELECT nom_grupo_capacidad, sum(cantidad)",
                                 [{"nom_grupo_capacidad": "CAMAS", "cantidad": "130", "sedes": "14"}]),
                                ("GROUP BY num_nivel_atencion, nom_grupo_capacidad", filas)])
    texto = correr(h.contar_capacidad(agrupar_por="nivel", capacidad="camas"))
    assert "Sin dato: 80 camas" in texto and "Nivel 3: 50 camas" in texto


def test_contar_departamento_ambiguo_pide_aclaracion():
    h, datos = herramientas()
    texto = correr(h.contar_capacidad(departamento="la"))
    assert datos.consultas == [] and "departamento" in texto.lower()


# ------------------------------- detalle_ips -------------------------------

def test_detalle_ips_busca_sin_tildes_y_lista_capacidades():
    capacidades = [{"nom_grupo_capacidad": "CAMAS", "nom_descripcion_capacidad": "Cuidado Intensivo Adulto",
                    "num_cantidad_capacidad_instalada": "20"},
                   {"nom_grupo_capacidad": "SALAS", "nom_descripcion_capacidad": "Quirófano",
                    "num_cantidad_capacidad_instalada": "12"}]
    h, datos = herramientas(reglas=[("c_digo_sede = '500102126'", capacidades),
                                    ("like", [SEDE_LAS_AMERICAS, SEDE_SAN_VICENTE])])
    texto = correr(h.detalle_ips(nombre="clinica las americas"))
    busqueda = next(q for q in datos.consultas if "like" in q)
    assert "'%CL_N_C_%'" in busqueda and "'%_M_R_C_S%'" in busqueda  # vocales → _ (acepta tildes)
    assert "CLÍNICA LAS AMERICAS" in texto and "Cuidado Intensivo Adulto: 20" in texto and "Quirófano: 12" in texto
    assert "3421010" in texto and "500102126-01" in texto


def test_detalle_ips_no_encontrada_lo_dice():
    h, _ = herramientas(reglas=[("like", [])])
    assert "No encontré" in correr(h.detalle_ips(nombre="clínica inexistente xyz"))


def test_detalle_ips_nombre_vacio():
    h, datos = herramientas()
    assert correr(h.detalle_ips(nombre="  ")).startswith("Error") and datos.consultas == []


# ------------------------------- describir_datos -------------------------------

def test_describir_datos_resume_la_fuente_con_calidad():
    h, _ = herramientas(reglas=[("count(*) AS sedes", [{"sedes": "15547"}]),
                                ("IS NULL", [{"n": "25266"}]),
                                ("GROUP BY naturaleza", [{"naturaleza": "Privada", "n": "25067"},
                                                          {"naturaleza": "Pública", "n": "16174"}])])
    texto = correr(h.describir_datos())
    assert "41.427 registros" in texto and "15.547 sedes" in texto and "61" in texto  # % sin nivel
    assert "nov" in texto.lower() and "no incluye horarios" in texto.lower()


# ------------------------------- contrato Tool + MCP -------------------------------

def test_expone_cuatro_tools_con_esquema():
    h, _ = herramientas()
    tools = {t.name: t for t in h.tools()}
    assert set(tools) == {"describir_datos", "buscar_ips", "contar_capacidad", "detalle_ips"}
    props = tools["contar_capacidad"].parameters["properties"]
    assert "departamento" in props["agrupar_por"]["enum"] and props["capacidad"]["type"] == "string"
    assert "Pública" in tools["buscar_ips"].parameters["properties"]["naturaleza"]["enum"]
    assert tools["detalle_ips"].parameters["required"] == ["nombre"]
    assert all(t.description for t in tools.values())


def test_las_tools_se_invocan_por_el_contrato_tool():
    h, _ = herramientas(reglas=[("count(*) AS sedes", [{"sedes": "1"}]), ("ORDER BY", [SEDE_LAS_AMERICAS])])
    buscar = next(t for t in h.tools() if t.name == "buscar_ips")
    assert "CLÍNICA LAS AMERICAS" in correr(buscar(municipio="Medellín", capacidad="cirugía"))


def test_servidor_mcp_expone_las_mismas_tools():
    from mcp import Client

    from mcp_servers import ips as servidor_ips

    h, _ = herramientas(reglas=[("count(*) AS sedes", [{"sedes": "1"}]), ("ORDER BY", [SEDE_LAS_AMERICAS])])
    mcp = servidor_ips.crear_servidor(h)

    async def usar():
        async with Client(mcp) as c:
            nombres = {t.name for t in (await c.list_tools()).tools}
            r = await c.call_tool("buscar_ips", {"municipio": "Medellín", "capacidad": "cirugía"})
            return nombres, r.content[0].text
    nombres, texto = correr(usar())
    assert nombres == {"describir_datos", "buscar_ips", "contar_capacidad", "detalle_ips"}
    assert "CLÍNICA LAS AMERICAS" in texto


# ------------------------------- integración real (opcional) -------------------------------

@pytest.mark.skipif("not config.getoption('--red')", reason="usa la API real: pytest --red")
def test_tools_contra_la_api_real():
    import time

    async def real():
        datos = D.ClienteDatosGov()
        try:
            h = T.HerramientasIPS(datos, await D.cargar_catalogo(datos))
            salidas = {}
            for nombre, coro in [("buscar", h.buscar_ips(municipio="Medellín", capacidad="cirugía")),
                                 ("contar", h.contar_capacidad(agrupar_por="departamento", capacidad="UCI")),
                                 ("detalle", h.detalle_ips(nombre="clinica las americas", municipio="Medellín")),
                                 ("describir", h.describir_datos())]:
                t = time.perf_counter()
                salidas[nombre] = (await coro, time.perf_counter() - t)
            return salidas
        finally:
            await datos.aclose()
    salidas = asyncio.run(real())
    for nombre, (texto, segundos) in salidas.items():
        assert not texto.startswith("Error"), (nombre, texto)
        assert segundos < 3, (nombre, segundos)
    assert "Valle del Cauca" in salidas["contar"][0]


# ---------------------- Mejoras tras probar con la API real ----------------------

def test_singular_para_una_sede():
    filas = [{"num_nivel_atencion": "3", "nom_grupo_capacidad": "CAMAS", "cantidad": "1", "sedes": "1"}]
    h, _ = herramientas(reglas=[("|> SELECT nom_grupo_capacidad, sum(cantidad)", filas),
                                ("GROUP BY num_nivel_atencion, nom_grupo_capacidad", filas)])
    texto = correr(h.contar_capacidad(agrupar_por="nivel", capacidad="camas"))
    assert "1 cama en 1 sede" in texto and "1 sedes" not in texto


def test_describir_datos_formatea_la_fecha_de_corte():
    h, _ = herramientas(reglas=[("GROUP BY fecha_corte", [{"fecha_corte": "Fecha corte REPS: Nov  5 2022  1:37PM"}])])
    texto = correr(h.describir_datos())
    assert "corte del 5 de noviembre de 2022" in texto and "1:37PM" not in texto


def test_detalle_ips_ambigua_sin_municipio_pide_aclaracion():
    otra = dict(SEDE_SAN_VICENTE, municipio="BARBOSA", c_digo_sede="507904077", n_mero_sede="01",
                nom_sede_ips="ESE HOSPITAL SAN VICENTE DE PAUL", nombre_prestador="ESE HOSPITAL SAN VICENTE DE PAUL")
    tercera = dict(otra, municipio="CALDAS", c_digo_sede="512900001",
                   nom_sede_ips="E.S.E HOSPITAL SAN VICENTE DE PAUL DE CALDAS")
    h, datos = herramientas(reglas=[("like", [otra, tercera, SEDE_SAN_VICENTE])])
    texto = correr(h.detalle_ips(nombre="hospital san vicente"))
    assert "varias" in texto.lower() and "BARBOSA" in texto and "MEDELLÍN" in texto
    assert not any("c_digo_sede = " in q for q in datos.consultas)  # no consultó el detalle de ninguna


def test_capacidad_desconocida_sin_sugerencias_utiles_dice_que_hay():
    h, _ = herramientas()
    texto = correr(h.buscar_ips(municipio="Medellín", capacidad="rayos x"))
    assert "rayos x" in texto.lower() and "camas" in texto.lower() and "salas" in texto.lower()
    assert "Quemados" not in texto


# ---------------------- Revisión QA de #8 ----------------------

TOTAL_Q = "|> SELECT nom_grupo_capacidad, sum(cantidad)"  # fragmento único de la consulta del total


def test_total_sale_de_su_propia_consulta_aunque_el_desglose_venga_truncado():
    desglose = [{"municipio": f"M{i}", "departamento": "Antioquia", "nom_grupo_capacidad": "AMBULANCIAS",
                 "cantidad": "1", "sedes": "1"} for i in range(T.LIMITE_DESGLOSE)]
    h, datos = herramientas(reglas=[(TOTAL_Q, [{"nom_grupo_capacidad": "AMBULANCIAS", "cantidad": "2231", "sedes": "900"}]),
                                    ("GROUP BY municipio, departamento, nom_grupo_capacidad", desglose)])
    texto = correr(h.contar_capacidad(agrupar_por="municipio", capacidad="ambulancias"))
    assert "Total: 2.231 ambulancias en 900 sedes" in texto
    assert "otros con menos capacidad" in texto
    assert any(f"LIMIT {T.LIMITE_DESGLOSE}" in q for q in datos.consultas)


def test_total_por_tipo_no_cuenta_sedes_repetidas():
    desglose = [{"nom_descripcion_capacidad": "Adultos", "nom_grupo_capacidad": "CAMAS", "cantidad": "5000", "sedes": "50"},
                {"nom_descripcion_capacidad": "Pediátrica", "nom_grupo_capacidad": "CAMAS", "cantidad": "1280", "sedes": "40"}]
    h, _ = herramientas(reglas=[(TOTAL_Q, [{"nom_grupo_capacidad": "CAMAS", "cantidad": "6280", "sedes": "53"}]),
                                ("GROUP BY nom_descripcion_capacidad, nom_grupo_capacidad", desglose)])
    texto = correr(h.contar_capacidad(agrupar_por="tipo", municipio="Medellín", capacidad="camas"))
    assert "Total: 6.280 camas en 53 sedes" in texto and "90 sedes" not in texto


@pytest.mark.parametrize("dicho,canon", [("publica", "Pública"), ("PÚBLICA", "Pública"), ("privada", "Privada"),
                                         ("Mixta", "Mixta")])
def test_naturaleza_se_normaliza(dicho, canon):
    h, datos = herramientas(reglas=[("count(*) AS sedes", [{"sedes": "1"}]), ("ORDER BY", [SEDE_SAN_VICENTE])])
    correr(h.buscar_ips(municipio="Medellín", naturaleza=dicho))
    assert all(f"naturaleza = '{canon}'" in q for q in datos.consultas)


@pytest.mark.parametrize("dicho,condicion", [("nivel 2", "num_nivel_atencion = '2'"), (2, "num_nivel_atencion = '2'"),
                                             ("sin dato", "num_nivel_atencion IS NULL")])
def test_nivel_se_normaliza(dicho, condicion):
    h, datos = herramientas(reglas=[("count(*) AS sedes", [{"sedes": "1"}]), ("ORDER BY", [SEDE_SAN_VICENTE])])
    correr(h.buscar_ips(municipio="Medellín", nivel=dicho))
    assert all(condicion in q for q in datos.consultas)


@pytest.mark.parametrize("campo,valor", [("naturaleza", "rara"), ("nivel", "altísimo")])
def test_valor_invalido_explica_sin_consultar(campo, valor):
    h, datos = herramientas()
    texto = correr(h.buscar_ips(municipio="Medellín", **{campo: valor}))
    assert texto.startswith("Error:") and campo in texto and datos.consultas == []


def test_parametros_raros_no_lanzan_excepciones():
    h, _ = herramientas(reglas=[("count(*) AS sedes", [{"sedes": "1"}]), ("ORDER BY", [SEDE_LAS_AMERICAS])])
    assert "CLÍNICA LAS AMERICAS" in correr(h.buscar_ips(municipio=None, departamento="Antioquia", limite="cinco"))


def test_un_bug_inesperado_se_devuelve_como_texto():
    h, datos = herramientas()

    async def explota(*a, **k):
        raise RuntimeError("bug")
    datos.consultar = explota
    assert correr(h.buscar_ips(municipio="Medellín")).startswith("Error: tuve un problema")


def test_buscar_sin_capacidad_ordena_por_capacidad_instalada():
    h, datos = herramientas(reglas=[("count(*) AS sedes", [{"sedes": "2"}]), ("ORDER BY", [SEDE_SAN_VICENTE])])
    correr(h.buscar_ips(municipio="Medellín"))
    assert any("ORDER BY cantidad DESC" in q and "|>" in q for q in datos.consultas)


def test_detalle_con_municipio_y_empate_pregunta_cual_sede():
    renal = dict(SEDE_SAN_VICENTE, c_digo_sede="999", nom_sede_ips="UNIDAD RENAL HOSPITAL SAN VICENTE",
                 nombre_prestador="FRESENIUS")
    h, datos = herramientas(reglas=[("like", [SEDE_SAN_VICENTE, renal])])
    texto = correr(h.detalle_ips(nombre="hospital san vicente", municipio="Medellín"))
    assert "¿Cuál sede?" in texto and "UNIDAD RENAL" in texto and "FUNDACION HOSPITALARIA SAN VICENTE DE PAUL" in texto
    assert not any("c_digo_sede = " in q for q in datos.consultas)


def test_detalle_nombre_generico_pide_mas_datos_sin_consultar():
    h, datos = herramientas()
    assert "muy general" in correr(h.detalle_ips(nombre="clínica")) and datos.consultas == []


def test_detalle_municipio_no_reconocido_lo_dice():
    h, datos = herramientas()
    texto = correr(h.detalle_ips(nombre="hospital san vicente", municipio="Narnia"))
    assert "No encontré el municipio 'Narnia'" in texto and datos.consultas == []


def test_detalle_busqueda_determinista():
    h, datos = herramientas(reglas=[("like", [])])
    correr(h.detalle_ips(nombre="las americas"))
    assert "ORDER BY nom_sede_ips" in datos.consultas[0]


def test_ficha_omite_el_prestador_si_repite_el_nombre():
    h, _ = herramientas(reglas=[("count(*) AS sedes", [{"sedes": "1"}]), ("ORDER BY", [SEDE_SAN_VICENTE])])
    texto = correr(h.buscar_ips(municipio="Medellín"))
    assert "(FUNDACION HOSPITALARIA SAN VICENTE DE PAUL)" not in texto


def test_patron_sin_tildes_acepta_la_enie():
    assert D.patron_sin_tildes("nariño") == "'%N_R___%'"


def test_mcp_conserva_las_descripciones_de_los_parametros():
    from mcp_servers import ips as servidor_ips
    h, _ = herramientas()
    mcp = servidor_ips.crear_servidor(h)
    props = mcp._tool_manager.get_tool("buscar_ips").parameters["properties"]
    assert "Medellín" in props["municipio"]["description"] and "máximo 10" in props["limite"]["description"]


def test_detalle_coincidencia_exacta_elige_la_sede_principal_y_menciona_las_demas():
    principal = dict(SEDE_LAS_AMERICAS, nom_sede_ips="HOSPITAL PABLO TOBON URIBE", c_digo_sede="500102104",
                     nombre_prestador="HOSPITAL PABLO TOBON URIBE")
    sede2 = dict(principal, n_mero_sede="02", nom_sede_ips="HOSPITAL PABLO TOBON URIBE SEDE BELEN")
    h, datos = herramientas(reglas=[("c_digo_sede = '500102104'", [{"nom_grupo_capacidad": "CAMAS",
                                     "nom_descripcion_capacidad": "Adultos", "num_cantidad_capacidad_instalada": "300"}]),
                                    ("like", [sede2, principal])])
    texto = correr(h.detalle_ips(nombre="pablo tobón uribe", municipio="Medellín"))
    assert texto.startswith("HOSPITAL PABLO TOBON URIBE ·") and "300 camas" in texto
    assert "También hay" in texto and "SEDE BELEN" in texto


def test_nivel_con_varios_valores_pide_aclaracion():
    h, datos = herramientas()
    assert correr(h.buscar_ips(municipio="Medellín", nivel="1 o 2")).startswith("Error:") and datos.consultas == []
