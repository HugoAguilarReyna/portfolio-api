"""Pruebas del proyecto «Atlas comercial · Demanda y ventas» (Walmart M5).

Se ejecutan contra MongoDB de pruebas (``hugo_portfolio_test``) y usan los CSV
**sintéticos** de ``tests/m5_fixture.py``: sirven para verificar ingesta,
reconciliación, filtros, concentración, baseline y contrato HTTP sin depender de la
descarga de Kaggle. Los datos reales se importan con ``scripts/importar_m5.py``.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.comercial import conexion, consultas, derivados, forecast, ingesta
from app.comercial.consultas import FiltroInvalido
from app.comercial.esquema import ErrorEsquema, Ventana, unidades_validas
from app.db.mongo import (
    M5_AGREGADOS,
    M5_BLOQUES,
    M5_CALENDARIO,
    M5_CATALOGO,
    M5_DERIVADOS,
    M5_EVALUACION,
    M5_IMPORTACIONES,
    M5_PRONOSTICOS,
)
from app.main import create_app
from tests import m5_fixture

COLECCIONES_M5 = (M5_IMPORTACIONES, M5_AGREGADOS, M5_BLOQUES, M5_CATALOGO, M5_CALENDARIO,
                  M5_PRONOSTICOS, M5_EVALUACION, M5_DERIVADOS)


@pytest.fixture()
def m5(mongo_db, tmp_path):
    """Importa el conjunto sintético en la base de pruebas (versión publicada)."""
    for nombre in COLECCIONES_M5:
        mongo_db[nombre].delete_many({})
    mongo_db["m5_estado"].delete_many({})
    datos = m5_fixture.escribir(tmp_path)
    manifiesto = ingesta.importar(mongo_db, tmp_path)
    yield {"db": mongo_db, "version": manifiesto["version"], "manifiesto": manifiesto, **datos}
    for nombre in COLECCIONES_M5:
        mongo_db[nombre].delete_many({})
    mongo_db["m5_estado"].delete_many({})


# --------------------------------------------------------------------------- #
# Ingesta y esquema
# --------------------------------------------------------------------------- #


def test_ingesta_idempotente(m5):
    """Reimportar el mismo contenido no duplica ni crea versión nueva."""
    db, version = m5["db"], m5["version"]
    bloques_antes = db[M5_BLOQUES].count_documents({"version": version})
    agregados_antes = db[M5_AGREGADOS].count_documents({"version": version})
    manifiesto = ingesta.importar(db, m5["carpeta"])
    assert manifiesto["version"] == version
    assert db[M5_BLOQUES].count_documents({"version": version}) == bloques_antes
    assert db[M5_AGREGADOS].count_documents({"version": version}) == agregados_antes
    assert db[M5_IMPORTACIONES].count_documents({"version": version}) == 1


def test_calendario_y_claves(m5):
    db = m5["db"]
    assert db[M5_CALENDARIO].count_documents({}) == m5["dias"]
    primero = db[M5_CALENDARIO].find_one({"d": 1})
    ultimo = db[M5_CALENDARIO].find_one({"d": m5["dias"]})
    assert primero["fecha"].date().isoformat() == m5_fixture.INICIO.isoformat()
    tiendas = [d["store_id"] for d in db[M5_CATALOGO].find({"tipo": "tienda"})]
    assert sorted(tiendas) == ["CA_1", "TX_1", "WI_1"]
    producto = db[M5_CATALOGO].find_one({"tipo": "producto"})
    assert producto["cat_id"] in {"FOODS", "HOBBIES"}
    assert len(m5["version"]) > 4


def test_reconciliacion_de_totales(m5):
    db, version = m5["db"], m5["version"]
    validaciones = m5["manifiesto"]["validaciones"]
    esperado = m5_fixture.total_esperado()
    assert validaciones["reconciliacion_total"] is True
    assert validaciones["reconciliacion_tiendas"] is True
    assert validaciones["suma_series"] == esperado
    agregado_total = db[M5_AGREGADOS].find_one({"dimension": "total", "clave": "todas", "version": version})
    assert sum(agregado_total["unidades"]) == esperado
    suma_tiendas = sum(
        sum(d["unidades"]) for d in db[M5_AGREGADOS].find({"dimension": "tienda", "version": version})
    )
    assert suma_tiendas == esperado


def test_cantidades_invalidas():
    assert unidades_validas("5") == 5
    for valor in (-1, "abc", float("nan"), float("inf"), 1.5):
        with pytest.raises(ErrorEsquema):
            unidades_validas(valor)


# --------------------------------------------------------------------------- #
# Filtros
# --------------------------------------------------------------------------- #


def test_filtros_invalidos(m5):
    db, version = m5["db"], m5["version"]
    with pytest.raises(FiltroInvalido):
        consultas.resolver_filtros(db, version, {"tiendas": ["NO_EXISTE"]})
    with pytest.raises(FiltroInvalido):
        consultas.resolver_filtros(db, version, {"categorias": ["NO_EXISTE"]})
    with pytest.raises(FiltroInvalido):
        consultas.resolver_filtros(
            db, version, {"desde": "2010-01-01", "hasta": "2011-03-01"}
        )
    with pytest.raises(FiltroInvalido):
        # A fuera del intervalo principal
        consultas.resolver_filtros(
            db,
            version,
            {"desde": "2011-02-01", "hasta": "2011-03-01",
             "a_desde": "2011-01-30", "a_hasta": "2011-01-31"},
        )
    with pytest.raises(FiltroInvalido):
        consultas.resolver_filtros(db, version, {"productos": ["NO_EXISTE"]})


def test_intervalo_invalido():
    with pytest.raises(ErrorEsquema):
        Ventana(desde=__import__("datetime").date(2011, 2, 2), hasta=__import__("datetime").date(2011, 2, 1))


# --------------------------------------------------------------------------- #
# Concentración y A/B
# --------------------------------------------------------------------------- #


def test_concentracion_monotona_y_cierre(m5):
    db, version = m5["db"], m5["version"]
    filtros = consultas.resolver_filtros(db, version, {"tiendas": ["CA_1"]})
    fechas = consultas.mapa_fechas(db)
    datos = consultas.concentracion(db, version, filtros=filtros, fechas=fechas, limite=100)
    assert datos["total"] > 0
    puntos = datos["puntos"]
    assert puntos[0]["porcentaje"] == 0 and puntos[0]["acumulado"] == 0
    acumulados = [p["porcentaje"] for p in puntos]
    assert acumulados == sorted(acumulados), "la curva debe ser monótona"
    assert math.isclose(puntos[-1]["porcentaje"], 100.0, abs_tol=1e-6)
    assert puntos[-1]["acumulado"] == datos["total"]


def test_concentracion_con_periodo_que_termina_antes_de_otro_bloque(m5):
    """Un periodo corto no debe romper la agregación por bloques posteriores.

    Defecto real: con un bloque *posterior* al intervalo, el ``$slice`` calculaba
    longitud 0 y MongoDB rechazaba la consulta (error 500) para cualquier periodo
    que terminara antes del último bloque. Aquí se añade un bloque futuro
    artificial y se comprueba que la suma del periodo sigue siendo la correcta.
    """
    db, version = m5["db"], m5["version"]
    referencia = db[M5_BLOQUES].find_one({"version": version, "store_id": "CA_1"}, {"_id": 0})
    db[M5_BLOQUES].insert_one(
        {
            **referencia,
            "_id": f"{version}:{referencia['item_id']}:CA_1:99",
            "bloque": 99,
            "d_inicio": m5_fixture.DIAS + 120,
            "d_fin": m5_fixture.DIAS + 315,
            "unidades": [7] * 196,
            "total": 7 * 196,
        }
    )

    filtros = consultas.resolver_filtros(
        db, version, {"tiendas": ["CA_1"], "desde": "2011-01-29", "hasta": "2011-02-07"}
    )
    datos = consultas.concentracion(db, version, filtros=filtros, fechas=consultas.mapa_fechas(db))
    esperado = sum(
        sum(documento["unidades"][:10])
        for documento in db[M5_BLOQUES].find(
            {"version": version, "store_id": "CA_1", "d_inicio": 1}, {"unidades": 1}
        )
    )
    assert datos["total"] == esperado
    assert datos["total"] < 7 * 196, "el bloque futuro no debe entrar en la suma"


def test_api_pulso_producto_con_periodo_sin_ventas(cliente, m5):
    """Sin productos con ventas positivas: estado concreto, sin 500 ni porcentajes."""
    db, version = m5["db"], m5["version"]
    derivados.construir(db, version)
    respuesta = cliente.get(
        "/api/v1/comercial/m5/pulso?nivel=producto&tienda=CA_1&departamento=FOODS_1"
        "&desde=2011-01-29&hasta=2011-01-29&agregacion=semana"
    )
    assert respuesta.status_code in (200, 422), respuesta.text[:400]
    if respuesta.status_code == 200:
        datos = respuesta.json()
        assert datos["contexto"]["productos_disponibles"] == 2
        # Sea cual sea la selección, el resumen y la concentración reconciliaron.
        assert datos["resumen"]["total"] == sum(serie["total"] for serie in datos["series"])


def test_concentracion_total_cero(m5):
    """Tienda con categoría sin ventas: estado vacío, sin divisiones inválidas."""
    db, version = m5["db"], m5["version"]
    filtros = consultas.resolver_filtros(
        db, version, {"tiendas": ["WI_1"], "categorias": ["HOBBIES"]}
    )
    fechas = consultas.mapa_fechas(db)
    datos = consultas.concentracion(db, version, filtros=filtros, fechas=fechas)
    assert datos["total"] == 0
    assert datos["estado"] == "vacio"
    assert datos["puntos"] == []


def test_comparacion_ab_y_variacion_no_calculable(m5):
    db, version = m5["db"], m5["version"]
    fechas = consultas.mapa_fechas(db)
    filtros = consultas.resolver_filtros(
        db,
        version,
        {
            "tiendas": ["CA_1"],
            "categorias": ["HOBBIES"],
            "a_desde": m5_fixture.INICIO.isoformat(),
            "a_hasta": "2011-01-31",
            "b_desde": "2011-02-01",
            "b_hasta": "2011-02-10",
        },
    )
    filas = consultas.comparacion_ab(db, version, filtros=filtros, fechas=fechas)
    hobbies = next(f for f in filas["filas"] if f["categoria"] == "HOBBIES")
    assert hobbies["a"] > 0
    assert hobbies["diferencia"] == hobbies["b"] - hobbies["a"]
    assert hobbies["variacion_pct"] is not None

    # A = 0 en la categoría que no vende en WI_1
    filtros_cero = consultas.resolver_filtros(
        db,
        version,
        {
            "tiendas": ["WI_1"],
            "categorias": ["HOBBIES"],
            "a_desde": m5_fixture.INICIO.isoformat(),
            "a_hasta": "2011-01-31",
            "b_desde": "2011-02-01",
            "b_hasta": "2011-02-10",
        },
    )
    filas_cero = consultas.comparacion_ab(db, version, filtros=filtros_cero, fechas=fechas)
    hobbies_cero = next(f for f in filas_cero["filas"] if f["categoria"] == "HOBBIES")
    assert hobbies_cero["a"] == 0
    assert hobbies_cero["variacion_pct"] is None
    assert "no calculable" in (hobbies_cero["nota"] or "").lower()


def test_resumen_reconcilia_con_concentracion(m5):
    db, version = m5["db"], m5["version"]
    filtros = consultas.resolver_filtros(db, version, {})
    fechas = consultas.mapa_fechas(db)
    series = consultas.series_por_tienda(db, version, filtros=filtros, agregacion="dia", fechas=fechas)
    concentracion = consultas.concentracion(db, version, filtros=filtros, fechas=fechas)
    resumen = consultas.resumen(series=series, filtros=filtros, concentracion_datos=concentracion)
    assert resumen["total"] == concentracion["total"]
    assert resumen["total"] == sum(serie["total"] for serie in series["series"])
    assert resumen["reconcilia_con_concentracion"] is True


# --------------------------------------------------------------------------- #
# Baseline
# --------------------------------------------------------------------------- #


def test_baseline_no_usa_futuro():
    """Cambiar los valores posteriores al origen no altera el pronóstico."""
    serie = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21]
    origen = 8
    original = forecast.predecir(serie, origen, 7)
    alterada = serie[:origen] + [999] * (len(serie) - origen)
    assert forecast.predecir(alterada, origen, 7) == original
    assert original == [11, 12, 13, 14, 15, 16, 17]


def test_metricas_y_agregado():
    pronostico = [10, 12, 8]
    observado = [11, 10, 8]
    metrica = forecast.metricas(pronostico, observado)
    assert metrica["n"] == 3
    assert metrica["mae"] == pytest.approx(round((1 + 2 + 0) / 3, 3))
    assert metrica["sesgo"] == pytest.approx(round(((-1) + 2 + 0) / 3, 3))
    assert metrica["wape"] == pytest.approx(round(100 * 3 / 29, 3))

    ventanas = [
        {"metricas": metrica, "_suma_abs": 3, "_suma_error": 1},
        {"metricas": metrica, "_suma_abs": 3, "_suma_error": 1},
    ]
    agregado = forecast.agregar_metricas(ventanas)
    # Dos ventanas idénticas: el agregado coincide con la métrica individual.
    assert agregado["n"] == 6
    assert agregado["mae"] == pytest.approx(metrica["mae"])
    assert agregado["sesgo"] == pytest.approx(metrica["sesgo"])
    assert agregado["wape"] == pytest.approx(metrica["wape"])
    assert agregado["denominador"] == 2 * metrica["denominador"]

    sin_base = {"n": 0, "mae": None, "sesgo": None, "wape": None, "denominador": 0,
                "denominador_cero": True}
    con_cero = forecast.agregar_metricas([*ventanas, {"metricas": sin_base, "_suma_abs": 0, "_suma_error": 0}])
    assert con_cero["ventanas_sin_base"] == 1
    assert con_cero["mae"] == pytest.approx(metrica["mae"])


def test_ventanas_de_backtesting(m5):
    db = m5["db"]
    fechas = consultas.mapa_fechas(db)
    origenes = forecast.ventanas_disponibles(fechas, 28, 3)
    assert origenes, "debe haber al menos una ventana con 70 días"
    ultimo = max(fechas)
    assert max(origenes) == ultimo - 28
    assert all(b - a == 28 for a, b in zip(origenes, origenes[1:]))


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


@pytest.fixture()
def cliente(m5, settings):
    app = create_app(settings)
    with TestClient(app) as cliente_http:
        yield cliente_http


def test_api_catalogo(cliente):
    respuesta = cliente.get("/api/v1/comercial/m5/catalogo?limite=2")
    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert len(datos["tiendas"]) == 3
    assert datos["productos_total"] == 3
    assert len(datos["productos"]) == 2
    assert datos["capacidades"]["ingresos"] is False


def test_api_pulso_y_reconciliacion(cliente):
    respuesta = cliente.get("/api/v1/comercial/m5/pulso?agregacion=semana")
    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["resumen"]["total"] == datos["concentracion"]["total"]
    assert datos["resumen"]["reconcilia_con_concentracion"] is True
    assert datos["dominio"]["agregacion"] == "semana"
    assert datos["parametros"]["agregacion"] == "semana"


def test_api_pulso_filtro_invalido(cliente):
    respuesta = cliente.get("/api/v1/comercial/m5/pulso?tiendas=NO_EXISTE")
    assert respuesta.status_code == 422
    assert respuesta.json()["detail"]["estado"] == "filtro_invalido"


def test_api_pronosticos_y_evaluacion(cliente, m5):
    db = m5["db"]
    forecast.calcular(db, m5["version"], ventanas=2)
    respuesta = cliente.get("/api/v1/comercial/m5/pronosticos?nivel=tienda&dias_historico=30")
    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["horizonte"] == 28
    assert datos["es_futuro"] is True
    assert len(datos["dias"]) == 28
    assert datos["series"], "debe haber series pronosticadas"

    evaluacion = cliente.get("/api/v1/comercial/m5/evaluacion?nivel=tienda")
    assert evaluacion.status_code == 200
    datos_eval = evaluacion.json()
    assert datos_eval["ventanas"]
    assert datos_eval["resumen"]["mae"] is not None
    assert "no es una evaluación rolling" in " ".join(datos_eval["limitaciones"]).lower()


def test_api_sin_datos_responde_pendiente(settings, mongo_db):
    for nombre in COLECCIONES_M5:
        mongo_db[nombre].delete_many({})
    mongo_db["m5_estado"].delete_many({})
    app = create_app(settings)
    with TestClient(app) as cliente_http:
        respuesta = cliente_http.get("/api/v1/comercial/m5/catalogo")
        assert respuesta.status_code == 503
        detalle = respuesta.json()["detail"]
        assert detalle["estado"] == "pendiente"
        assert "importar" in detalle["accion"].lower()
        estado = cliente_http.get("/api/v1/comercial/m5/estado").json()
        assert estado["con_datos"] is False
        assert estado["estado"] == "pendiente"


# --------------------------------------------------------------------------- #
# Exploración: tiendas → departamentos → productos
# --------------------------------------------------------------------------- #


def test_derivado_departamento_tienda_idempotente(m5):
    """El derivado se construye una vez, reconcilia y se reutiliza sin recalcular."""
    db, version = m5["db"], m5["version"]
    resultado = derivados.construir(db, version)
    assert resultado["estado"] == "publicado"
    assert resultado["reutilizado"] is False
    assert resultado["reconciliacion"]["ok"] is True
    # 3 tiendas × 2 departamentos del conjunto sintético: no se fuerza ninguna cifra.
    assert resultado["documentos"] == 6
    assert db[M5_AGREGADOS].count_documents({"version": version, "dimension": "departamento_tienda"}) == 6
    for documento in db[M5_AGREGADOS].find({"dimension": "departamento_tienda"}):
        assert documento["dias_observados"] == m5["dias"]
        assert all(valor >= 0 for valor in documento["unidades"])

    repetido = derivados.construir(db, version)
    assert repetido["reutilizado"] is True
    assert repetido["documentos"] == 6
    assert db[M5_AGREGADOS].count_documents({"dimension": "departamento_tienda"}) == 6


def test_derivado_reconcilia_con_tienda_y_departamento(m5):
    """Suma de departamentos por día = agregado de la tienda; ídem por departamento."""
    db, version = m5["db"], m5["version"]
    derivados.construir(db, version)
    derivadas = list(db[M5_AGREGADOS].find({"version": version, "dimension": "departamento_tienda"}))
    por_tienda: dict[str, list[int]] = {}
    por_departamento: dict[str, list[int]] = {}
    for documento in derivadas:
        destino_t = por_tienda.setdefault(documento["store_id"], [0] * len(documento["unidades"]))
        destino_d = por_departamento.setdefault(documento["dept_id"], [0] * len(documento["unidades"]))
        for indice, valor in enumerate(documento["unidades"]):
            destino_t[indice] += valor
            destino_d[indice] += valor
    for tienda, valores in por_tienda.items():
        esperado = db[M5_AGREGADOS].find_one(
            {"version": version, "dimension": "tienda", "clave": tienda}, {"unidades": 1}
        )
        assert valores == esperado["unidades"], f"la tienda {tienda} no reconcilia"
    for departamento, valores in por_departamento.items():
        esperado = db[M5_AGREGADOS].find_one(
            {"version": version, "dimension": "departamento", "clave": {"$regex": f"\\|{departamento}$"}},
            {"unidades": 1},
        )
        assert valores == esperado["unidades"], f"el departamento {departamento} no reconcilia"


def test_derivado_sin_publicar_calcula_desde_bloques(m5):
    """Un derivado no publicado nunca se sirve a medias: se recalcula desde bloques."""
    db, version = m5["db"], m5["version"]
    derivados.construir(db, version)
    publicado = derivados.series(db, version, tienda="CA_1", departamentos=["FOODS_1", "HOBBIES_1"])

    db[M5_DERIVADOS].update_one({"version": version}, {"$set": {"estado": derivados.ESTADO_FALLO}})
    db[M5_AGREGADOS].delete_many({"version": version, "dimension": "departamento_tienda"})
    assert derivados.publicado(db, version) is False
    respaldo = derivados.series(db, version, tienda="CA_1", departamentos=["FOODS_1", "HOBBIES_1"])
    assert respaldo == publicado


def test_api_pulso_departamentos_reconcilia_con_tienda(cliente, m5):
    db, version = m5["db"], m5["version"]
    derivados.construir(db, version)
    respuesta = cliente.get("/api/v1/comercial/m5/pulso?nivel=departamento&tienda=CA_1&agregacion=semana")
    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["nivel"] == "departamento"
    assert datos["contexto"]["tienda"] == "CA_1"
    assert datos["derivado"]["publicado"] is True
    assert {serie["clave"] for serie in datos["series"]} == {"FOODS_1", "HOBBIES_1"}
    assert datos["migas"][-1]["nivel"] == "departamento"
    assert datos["cobertura"]["dias"] == m5["dias"]

    tiendas = cliente.get("/api/v1/comercial/m5/pulso?tiendas=CA_1&agregacion=semana").json()
    assert datos["resumen"]["total"] == tiendas["resumen"]["total"]
    assert datos["resumen"]["reconcilia_con_concentracion"] is True
    assert datos["concentracion"]["alcance"].startswith("productos con ventas positivas de CA_1")
    assert datos["resumen"]["total_contexto"] == datos["resumen"]["total_seleccion"]


def test_api_pulso_departamentos_respeta_filtro_de_categorias(cliente, m5):
    """Al descender no se rellenan paneles con categorías excluidas."""
    derivados.construir(m5["db"], m5["version"])
    datos = cliente.get(
        "/api/v1/comercial/m5/pulso?nivel=departamento&tienda=CA_1&categorias=HOBBIES"
    ).json()
    assert [serie["clave"] for serie in datos["series"]] == ["HOBBIES_1"]
    assert [fila["clave"] for fila in datos["comparacion"]["filas"]] == ["HOBBIES_1"]
    assert datos["contexto"]["departamentos"] == ["HOBBIES_1"]

    sin_paneles = cliente.get(
        "/api/v1/comercial/m5/pulso?nivel=departamento&tienda=CA_1&categorias=HOBBIES&departamentos=FOODS_1"
    )
    assert sin_paneles.status_code == 422


def test_api_pulso_validaciones_de_nivel(cliente, m5):
    """Un contexto incompleto o contradictorio es 422, nunca «todos»."""
    derivados.construir(m5["db"], m5["version"])
    casos = [
        "nivel=departamento",
        "nivel=departamento&tienda=NO_EXISTE",
        "nivel=departamento&tienda=TX_1&tiendas=CA_1",
        "nivel=tienda&tienda=CA_1",
        "nivel=tienda&departamento=FOODS_1",
        "nivel=departamento&tienda=CA_1&departamento=FOODS_1",
        "nivel=departamento&tienda=CA_1&productos=FOODS_1_001",
        "nivel=producto&tienda=CA_1",
        "nivel=producto&tienda=CA_1&departamento=NO_EXISTE",
        "nivel=producto&tienda=CA_1&departamento=FOODS_1&productos=HOBBIES_1_001",
        "nivel=producto&tienda=CA_1&departamento=FOODS_1&categorias=HOBBIES",
        "nivel=producto&tienda=CA_1&departamento=FOODS_1&productos=" + "&productos=".join(["P"] * 6),
    ]
    for consulta in casos:
        respuesta = cliente.get(f"/api/v1/comercial/m5/pulso?{consulta}")
        assert respuesta.status_code == 422, consulta
        assert respuesta.json()["detail"]["estado"] == "filtro_invalido", consulta

    # Un nivel fuera de la enumeración lo rechaza la validación del contrato (422).
    assert cliente.get("/api/v1/comercial/m5/pulso?nivel=otro").status_code == 422


def test_api_pulso_productos_seleccion_inicial_y_alcance(cliente, m5):
    """Cinco (o los disponibles) por volumen, con el total del departamento aparte."""
    db, version = m5["db"], m5["version"]
    derivados.construir(db, version)

    automatica = cliente.get(
        "/api/v1/comercial/m5/pulso?nivel=producto&tienda=CA_1&departamento=FOODS_1"
    ).json()
    assert automatica["nivel"] == "producto"
    assert automatica["contexto"]["seleccion_automatica"] is True
    assert automatica["contexto"]["departamento"] == "FOODS_1"
    assert automatica["contexto"]["productos_disponibles"] == 2
    assert len(automatica["series"]) == 2  # menos de cinco disponibles: se muestran los 2
    assert automatica["resumen"]["total_contexto"] == automatica["resumen"]["total_seleccion"]
    assert automatica["concentracion"]["total"] == automatica["resumen"]["total"]
    assert [fila["nivel"] for fila in automatica["comparacion"]["filas"]] == ["producto", "producto"]

    explicita = cliente.get(
        "/api/v1/comercial/m5/pulso?nivel=producto&tienda=CA_1&departamento=FOODS_1"
        "&productos=FOODS_1_001"
    ).json()
    assert explicita["contexto"]["seleccion_automatica"] is False
    assert explicita["resumen"]["total_seleccion"] < explicita["resumen"]["total_contexto"]
    assert explicita["resumen"]["participacion_pct"] is not None
    assert explicita["concentracion"]["productos"] == 1
    assert explicita["concentracion"]["estado"] == "unico"
    assert [fila["clave"] for fila in explicita["comparacion"]["filas"]] == ["FOODS_1_001"]


def test_api_pulso_producto_reconcilia_con_bloques(cliente, m5):
    """La serie de un producto reconciliación con sus bloques locales."""
    db, version = m5["db"], m5["version"]
    derivados.construir(db, version)
    datos = cliente.get(
        "/api/v1/comercial/m5/pulso?nivel=producto&tienda=CA_1&departamento=FOODS_1"
        "&productos=FOODS_1_002&agregacion=dia"
    ).json()
    serie = next(s for s in datos["series"] if s["clave"] == "FOODS_1_002")
    suma_bloques = sum(
        sum(documento["unidades"])
        for documento in db[M5_BLOQUES].find(
            {"version": version, "store_id": "CA_1", "item_id": "FOODS_1_002"}, {"unidades": 1}
        )
    )
    assert serie["total"] == suma_bloques
    assert datos["resumen"]["total"] == suma_bloques


def test_api_productos_busqueda_paginada(cliente, m5):
    derivados.construir(m5["db"], m5["version"])
    base = "/api/v1/comercial/m5/productos?tienda=CA_1&departamento=FOODS_1"
    primera = cliente.get(f"{base}&limite=1").json()
    assert primera["total"] == 2
    assert len(primera["productos"]) == 1
    assert primera["paginacion"]["hay_mas"] is True
    assert len(primera["seleccion_inicial"]) == 2
    assert all(item["unidades"] > 0 for item in primera["seleccion_inicial"])

    segunda = cliente.get(f"{base}&limite=1&desplazamiento=1").json()
    assert segunda["productos"][0]["item_id"] != primera["productos"][0]["item_id"]
    assert segunda["paginacion"]["hay_mas"] is False

    por_identificador = cliente.get(f"{base}&orden=item_id").json()
    assert [p["item_id"] for p in por_identificador["productos"]] == ["FOODS_1_001", "FOODS_1_002"]

    con_texto = cliente.get(f"{base}&texto=002").json()
    assert [p["item_id"] for p in con_texto["productos"]] == ["FOODS_1_002"]
    assert con_texto["total"] == 1

    assert cliente.get(f"{base}&orden=otro").status_code == 422
    assert cliente.get(
        "/api/v1/comercial/m5/productos?tienda=CA_1&departamento=FOODS_1&categorias=HOBBIES"
    ).status_code == 422
    assert cliente.get("/api/v1/comercial/m5/productos?tienda=NO_EXISTE").status_code == 422


def test_estado_publica_el_derivado(cliente, m5):
    db, version = m5["db"], m5["version"]
    sin_derivado = cliente.get("/api/v1/comercial/m5/estado").json()
    assert sin_derivado["derivado"] is None or sin_derivado["derivado"]["publicado"] is False
    derivados.construir(db, version)
    con_derivado = cliente.get("/api/v1/comercial/m5/estado").json()
    assert con_derivado["derivado"]["publicado"] is True
    assert con_derivado["derivado"]["documentos"] == 6
    catalogo = cliente.get("/api/v1/comercial/m5/catalogo?limite=1").json()
    assert catalogo["capacidades"]["derivado_departamento_tienda"] is True
    assert catalogo["capacidades"]["niveles_exploracion"] == ["tienda", "departamento", "producto"]


# --------------------------------------------------------------------------- #
# Territorio, composición y comparación histórica
# --------------------------------------------------------------------------- #


def test_panorama_reconcilia_estados_y_total(cliente, m5):
    """Cada estado suma sus tiendas y los estados suman el total del contexto."""
    db, version = m5["db"], m5["version"]
    derivados.construir(db, version)
    datos = cliente.get("/api/v1/comercial/m5/pulso?agregacion=semana").json()
    panorama = datos["panorama"]
    assert panorama is not None
    assert panorama["estados_incluidos"] == 3
    assert panorama["estados_catalogo"] == 3
    total_estados = sum(estado["unidades"] for estado in panorama["estados"])
    assert total_estados == panorama["total_unidades"] == datos["resumen"]["total"]
    assert all(estado["tiendas_incluidas"] == 1 for estado in panorama["estados"])
    participacion = sum(estado["participacion_pct"] or 0 for estado in panorama["estados"])
    assert abs(participacion - 100) < 0.2

    # Suma de tiendas = estado: cada tienda aparece una sola vez.
    tiendas_vistas = [tienda for estado in panorama["estados"] for tienda in estado["tiendas"]]
    assert sorted(tiendas_vistas) == ["CA_1", "TX_1", "WI_1"]

    # Con filtro de categoría, el panorama respeta los filtros comerciales.
    hobbies = cliente.get("/api/v1/comercial/m5/pulso?categorias=HOBBIES&agregacion=semana").json()
    total_hobbies = sum(estado["unidades"] for estado in hobbies["panorama"]["estados"])
    assert total_hobbies == hobbies["resumen"]["total"]


def test_composicion_denominadores_explicitos(cliente, m5):
    """Categorías sobre el total del contexto; departamentos dentro de su categoría."""
    db, version = m5["db"], m5["version"]
    derivados.construir(db, version)
    datos = cliente.get("/api/v1/comercial/m5/pulso?agregacion=semana").json()
    composicion = datos["composicion"]
    assert composicion is not None
    assert [fila["codigo"] for fila in composicion["categorias"]] == ["FOODS", "HOBBIES"]
    total = sum(fila["unidades"] for fila in composicion["categorias"])
    assert total == composicion["total_unidades"] == datos["resumen"]["total"]

    por_categoria: dict[str, int] = {}
    for fila in composicion["departamentos"]:
        por_categoria[fila["categoria"]] = por_categoria.get(fila["categoria"], 0) + fila["unidades"]
        assert fila["denominador"].startswith("total de")
    for fila in composicion["categorias"]:
        assert por_categoria[fila["codigo"]] == fila["unidades"], "departamentos deben sumar la categoría"

    # La composición es el selector: no se reduce a la categoría ya elegida.
    filtrado = cliente.get("/api/v1/comercial/m5/pulso?categorias=HOBBIES&agregacion=semana").json()
    assert len(filtrado["composicion"]["categorias"]) == 2
    assert filtrado["resumen"]["total"] < filtrado["composicion"]["total_unidades"]

    # Un contexto territorial reduce el universo: CA_1 no incluye TX_1 ni WI_1.
    ca = cliente.get(
        "/api/v1/comercial/m5/pulso?nivel=departamento&tienda=CA_1&agregacion=semana"
    ).json()
    assert ca["composicion"]["total_unidades"] < composicion["total_unidades"]


def test_historico_sin_referencia_con_un_solo_anio(cliente, m5):
    """Con 70 días de 2011 no hay años completos anteriores: nada de banda."""
    datos = cliente.get("/api/v1/comercial/m5/historico?anio=2011&modo=semana").json()
    assert datos["sin_referencia"] is True
    assert datos["referencias"]["anios_incluidos"] == []
    assert datos["referencias"]["n_anios"] == 0
    assert datos["nota_banda"].startswith("Rango histórico observado")
    assert "no es un intervalo de confianza" in datos["nota_banda"].lower()
    con_dato = [punto for punto in datos["puntos"] if punto["observado"] is not None]
    assert con_dato, "la serie observada debe estar"
    assert all(punto["referencia_promedio"] is None for punto in datos["puntos"])
    assert all(punto["referencia_min"] is None for punto in datos["puntos"])


# --------------------------------------------------------------------------- #
# Comparación histórica con varios años (conjunto largo declarado sintético)
# --------------------------------------------------------------------------- #

INICIO_LARGO = date(2011, 12, 26)
DIAS_LARGO = (date(2015, 12, 31) - INICIO_LARGO).days + 1


@pytest.fixture()
def m5_largo(mongo_db, tmp_path):
    """Conjunto sintético de cuatro años completos y un inicio parcial (2011)."""
    for nombre in COLECCIONES_M5:
        mongo_db[nombre].delete_many({})
    mongo_db["m5_estado"].delete_many({})
    datos = m5_fixture.escribir(tmp_path, inicio=INICIO_LARGO, dias=DIAS_LARGO)
    manifiesto = ingesta.importar(mongo_db, tmp_path)
    yield {"db": mongo_db, "version": manifiesto["version"], "manifiesto": manifiesto, **datos}
    for nombre in COLECCIONES_M5:
        mongo_db[nombre].delete_many({})
    mongo_db["m5_estado"].delete_many({})


@pytest.fixture()
def cliente_largo(m5_largo, settings):
    app = create_app(settings)
    with TestClient(app) as cliente_http:
        yield cliente_http


def test_historico_usa_solo_anios_anteriores_completos(cliente_largo):
    """Referencias: años anteriores completos; los incompletos se excluyen y se dice."""
    datos = cliente_largo.get("/api/v1/comercial/m5/historico?anio=2015&modo=semana").json()
    assert datos["anios_disponibles"] == [2011, 2012, 2013, 2014, 2015]
    assert datos["referencias"]["anios_incluidos"] == [2012, 2013, 2014]
    excluidos = {fila["anio"]: fila["motivo"] for fila in datos["referencias"]["anios_excluidos"]}
    assert 2011 in excluidos and "incompleta" in excluidos[2011].lower()
    assert all(anio < 2015 for anio in datos["referencias"]["anios_incluidos"])
    assert 2015 not in datos["referencias"]["anios_incluidos"]
    assert "criterio" in datos["referencias"]

    con_referencia = [p for p in datos["puntos"] if p["referencias"] > 0]
    assert con_referencia, "debe haber semanas con referencia"
    assert all(p["referencia_min"] <= p["referencia_promedio"] <= p["referencia_max"] for p in con_referencia)
    assert all(p["diferencia"] == pytest.approx(p["observado"] - p["referencia_promedio"]) for p in con_referencia)


def test_historico_una_referencia_y_sin_referencia(cliente_largo):
    """2013 tiene una sola referencia (2012); 2012 no tiene ninguna."""
    una = cliente_largo.get("/api/v1/comercial/m5/historico?anio=2013&modo=semana").json()
    assert una["referencias"]["anios_incluidos"] == [2012]
    assert una["una_referencia"] is True
    assert una["sin_referencia"] is False
    puntos = [p for p in una["puntos"] if p["referencias"] > 0]
    assert puntos
    # Con una sola referencia, mínimo = máximo = promedio: no es evidencia de variabilidad.
    assert all(p["referencia_min"] == p["referencia_max"] == round(p["referencia_promedio"]) for p in puntos)

    ninguna = cliente_largo.get("/api/v1/comercial/m5/historico?anio=2012&modo=semana").json()
    assert ninguna["referencias"]["anios_incluidos"] == []
    assert ninguna["sin_referencia"] is True


def test_historico_semana_53_sin_referencias(cliente_largo):
    """2015 tiene semana 53 ISO; las referencias no, y se declara con 0 referencias."""
    datos = cliente_largo.get("/api/v1/comercial/m5/historico?anio=2015&modo=semana").json()
    ultima = datos["puntos"][-1]
    assert ultima["semana"] == 53
    assert ultima["referencias"] == 0
    assert ultima["referencia_promedio"] is None
    assert ultima["estado"] in ("ok", "sin_referencia")
    assert ultima["fecha_desde"].startswith("2015-12-28")


def test_historico_acumulado_reconcilia_con_unidades_diarias(cliente_largo, m5_largo):
    """El acumulado del año es la suma corrida de las unidades diarias del contexto."""
    datos = cliente_largo.get("/api/v1/comercial/m5/historico?anio=2013&modo=acumulado").json()
    assert datos["acumulado"]["completo"] is True
    assert datos["acumulado"]["desde"] == "2013-01-01"
    assert datos["referencias"]["anios_incluidos"] == [2012]

    db, version = m5_largo["db"], m5_largo["version"]
    filtros = consultas.resolver_filtros(db, version, {})
    fechas = consultas.mapa_fechas(db)
    dias = list(range(1, DIAS_LARGO + 1))
    vector = consultas._vector_contexto(
        db,
        version,
        tiendas=filtros["tiendas"],
        categorias=filtros["categorias"],
        departamentos=filtros["departamentos"],
        dias=dias,
    )
    por_fecha = {
        fechas[dia]: valor for dia, valor in zip(dias, vector, strict=True) if valor is not None
    }
    acumulado_manual = 0
    esperado: dict[str, int] = {}
    cursor = date(2013, 1, 1)
    while cursor <= date(2013, 12, 31):
        acumulado_manual += por_fecha.get(cursor, 0)
        esperado[cursor.isoformat()] = acumulado_manual
        cursor += timedelta(days=1)

    puntos = {punto["fecha_hasta"]: punto for punto in datos["puntos"]}
    for fecha in ("2013-01-01", "2013-03-01", "2013-06-15", "2013-12-31"):
        assert puntos[fecha]["observado"] == esperado[fecha]

    # La referencia usa el acumulado real del año anterior hasta la misma fecha. 2012
    # es bisiesto: hasta el 1 de marzo suma un día más (29 de febrero) y eso se declara.
    primero_marzo = puntos["2013-03-01"]
    dias_2012 = (date(2012, 3, 1) - date(2012, 1, 1)).days + 1
    esperado_ref = sum(
        por_fecha.get(date(2012, 1, 1) + timedelta(days=desplazamiento), 0)
        for desplazamiento in range(dias_2012)
    )
    assert dias_2012 == 61
    assert primero_marzo["referencia_promedio"] == pytest.approx(esperado_ref)
    assert primero_marzo["referencias"] == 1


def test_historico_acumulado_parcial_2011(cliente_largo):
    """2011 no empieza el 1 de enero: el acumulado se rotula como inicio disponible."""
    datos = cliente_largo.get("/api/v1/comercial/m5/historico?anio=2011&modo=acumulado").json()
    assert datos["acumulado"]["completo"] is False
    assert datos["acumulado"]["desde"] == INICIO_LARGO.isoformat()
    assert "inicio disponible" in datos["acumulado"]["etiqueta"].lower()
    assert datos["sin_referencia"] is True
    assert datos["puntos"][0]["fecha_desde"] == INICIO_LARGO.isoformat()


def test_historico_acumulado_no_completo_si_termina_antes_de_diciembre():
    """Un año que empieza el 1 de enero pero termina antes no es acumulado anual completo."""
    inicio_cobertura = date(2014, 1, 1)
    fin_cobertura = date(2014, 5, 22)
    unidades = {}
    cursor = inicio_cobertura
    while cursor <= fin_cobertura:
        unidades[cursor] = 3
        cursor += timedelta(days=1)
    puntos, acumulado = consultas._puntos_acumulado(
        anio=2014,
        unidades=unidades,
        referencias=[],
        desde=inicio_cobertura,
        hasta=fin_cobertura,
    )
    assert acumulado["completo"] is False
    assert acumulado["desde"] == "2014-01-01"
    assert "último dato" in acumulado["etiqueta"].lower()
    assert "2024" not in acumulado["etiqueta"]
    fin = next(punto for punto in puntos if punto["fecha_desde"] == "2014-05-22")
    assert fin["observado"] == 3 * (31 + 28 + 31 + 30 + 22)
    # Después del último dato la serie observada queda ausente, no en cero.
    siguiente = next(punto for punto in puntos if punto["fecha_desde"] == "2014-05-23")
    assert siguiente["observado"] is None
    assert siguiente["estado"] == "ausente"


def test_historico_anotaciones_no_redundantes(cliente_largo):
    """Como máximo dos hallazgos y sin repetir el mismo cierre en el acumulado."""
    semana = cliente_largo.get("/api/v1/comercial/m5/historico?anio=2015&modo=semana").json()
    assert 1 <= len(semana["anotaciones"]) <= 2
    acumulado = cliente_largo.get("/api/v1/comercial/m5/historico?anio=2015&modo=acumulado").json()
    assert 1 <= len(acumulado["anotaciones"]) <= 2
    tipos = [anotacion["tipo"] for anotacion in acumulado["anotaciones"]]
    assert tipos[0] == "cierre"


def test_historico_valida_anio_y_modo(cliente_largo):
    assert cliente_largo.get("/api/v1/comercial/m5/historico?anio=2010&modo=semana").status_code == 422
    assert cliente_largo.get("/api/v1/comercial/m5/historico?anio=2015&modo=otro").status_code == 422
    assert cliente_largo.get("/api/v1/comercial/m5/historico?modo=semana").status_code == 422


def test_historico_acumulado_bisiesto_solo_referencias_con_esa_fecha():
    """El 29 de febrero solo promedia referencias bisiestas; nunca interpola."""
    unidades = {}
    cursor = date(2012, 1, 1)
    while cursor <= date(2016, 12, 31):
        unidades[cursor] = 10
        cursor += timedelta(days=1)
    puntos, _ = consultas._puntos_acumulado(
        anio=2016,
        unidades=unidades,
        referencias=[2012, 2013],
        desde=date(2012, 1, 1),
        hasta=date(2016, 12, 31),
    )
    por_clave = {punto["mes_dia"]: punto for punto in puntos}
    bisiesto = por_clave["02-29"]
    assert bisiesto["observado"] is not None
    assert bisiesto["referencias"] == 1  # solo 2012 tiene 29 de febrero
    # Acumulado del 29 de febrero de 2016: 31 + 29 días × 10 unidades.
    assert bisiesto["observado"] == 60 * 10

    # La referencia de 2012 acumula hasta su 29 de febrero (31 + 29 días).
    assert bisiesto["referencia_promedio"] == pytest.approx(60 * 10)
    # El 28 de febrero de 2016 compara con ambos años.
    assert por_clave["02-28"]["referencias"] == 2


def test_historico_semana_parcial_no_se_compara():
    """Una semana cortada por la cobertura no se compara con semanas completas."""
    desde = date(2013, 1, 1)
    hasta = date(2014, 12, 24)  # miércoles: la última semana ISO de 2014 queda partida
    unidades = {}
    cursor = desde
    while cursor <= hasta:
        unidades[cursor] = 1
        cursor += timedelta(days=1)
    puntos = consultas._puntos_semana(
        anio=2014, unidades=unidades, referencias=[2013], desde=desde, hasta=hasta
    )
    ultima = next(punto for punto in puntos if punto["semana"] == 52)
    assert ultima["parcial"] is True
    assert ultima["referencia_promedio"] is None
    assert "parcial" in (ultima["motivo"] or "").lower()

    # Una semana completa del mismo año sí compara con la referencia.
    completa = next(punto for punto in puntos if punto["semana"] == 20)
    assert completa["parcial"] is False
    assert completa["referencias"] == 1
    assert completa["referencia_promedio"] == pytest.approx(7)  # una unidad por día


def test_historico_no_usa_anios_futuros(cliente_largo):
    """Cambiar el año analizado nunca incorpora años posteriores como referencia."""
    for anio in (2012, 2013, 2014, 2015):
        datos = cliente_largo.get(f"/api/v1/comercial/m5/historico?anio={anio}&modo=semana").json()
        assert all(referencia < anio for referencia in datos["referencias"]["anios_incluidos"])
        assert datos["anios_disponibles"] == [2011, 2012, 2013, 2014, 2015]


def test_productos_participacion_sobre_el_universo(cliente, m5):
    """La participación se calcula sobre el departamento, no sobre la página."""
    derivados.construir(m5["db"], m5["version"])
    datos = cliente.get(
        "/api/v1/comercial/m5/productos?tienda=CA_1&departamento=FOODS_1&limite=1"
    ).json()
    universo = datos["universo"]["unidades"]
    assert universo > 0
    fila = datos["productos"][0]
    assert fila["participacion_pct"] == pytest.approx(round(100 * fila["unidades"] / universo, 4))
    conjunto = datos["seleccion_inicial_participacion_pct"]
    assert conjunto is not None and 0 < conjunto <= 100
    # La suma de la selección no es el total del departamento.
    suma_seleccion = sum(item["unidades"] for item in datos["seleccion_inicial"])
    assert suma_seleccion <= universo


def test_pulso_contexto_de_estado_agrega_sus_tiendas(cliente_largo):
    """`estado` es contexto territorial: departamentos y productos suman sus tiendas."""
    datos = cliente_largo.get(
        "/api/v1/comercial/m5/pulso?nivel=departamento&estado=CA&agregacion=semana"
    ).json()
    assert datos["contexto"]["estado"] == "CA"
    assert datos["contexto"]["tiendas_contexto"] == ["CA_1"]
    assert {serie["clave"] for serie in datos["series"]} == {"FOODS_1", "HOBBIES_1"}

    estados = cliente_largo.get(
        "/api/v1/comercial/m5/pulso?nivel=departamento&agregacion=semana"
    )
    assert estados.status_code == 422, "sin contexto territorial explícito debe fallar"

    inexistente = cliente_largo.get("/api/v1/comercial/m5/pulso?estado=ZZ&agregacion=semana")
    assert inexistente.status_code == 422


# --------------------------------------------------------------------------- #
# Destino: solo MongoDB local del proyecto (nunca la URI principal)
# --------------------------------------------------------------------------- #

def test_destino_local_valido():
    uri, base = conexion.resolver_destino("mongodb://127.0.0.1:27017/atlas_comercial_local")
    assert base == "atlas_comercial_local"
    assert conexion.describir(uri) == "127.0.0.1:27017/atlas_comercial_local"
    assert "mongodb://" not in conexion.describir(uri)  # sin cadena de conexión completa


def test_destino_docker_m5_exclusivo():
    uri = "mongodb://mongo-m5:27017/atlas_comercial_local"
    assert conexion.resolver_destino(uri) == (uri, "atlas_comercial_local")
    for invalida in (
        "mongodb://mongo-m5:27018/atlas_comercial_local",
        "mongodb://mongo-m5:27017/hugo_portfolio",
        "mongodb://mongo-m5.example.com:27017/atlas_comercial_local",
        "mongodb+srv://mongo-m5/atlas_comercial_local",
    ):
        with pytest.raises(conexion.DestinoNoPermitido):
            conexion.resolver_destino(invalida)


def test_destino_rechaza_remotos_sin_abrir_red(monkeypatch):
    """Rechazo puro: si alguien intentara crear un cliente, la prueba falla."""
    import pymongo

    def _no_permitido(*args, **kwargs):  # pragma: no cover - debe no invocarse
        raise AssertionError("no debe crearse un cliente para un destino rechazado")

    monkeypatch.setattr(pymongo, "MongoClient", _no_permitido)
    for remota in (
        "mongodb+srv://cluster0.kpdhd5e.mongodb.net/hugo",
        "mongodb://cluster0.kpdhd5e.mongodb.net:27017/hugo",
        "mongodb://10.0.0.5:27017/hugo",
        "http://127.0.0.1:27017/hugo",
        "",
    ):
        with pytest.raises(conexion.DestinoNoPermitido):
            conexion.resolver_destino(remota)


def test_destino_contrdiccion_de_base():
    with pytest.raises(conexion.DestinoNoPermitido):
        conexion.resolver_destino(
            "mongodb://127.0.0.1:27017/atlas_comercial_local", "otra_base"
        )


def test_comercial_no_hereda_uri_principal(settings):
    """La configuración del proyecto no puede apuntar a la base del portafolio."""
    from app.comercial import conexion as cx

    uri, base = cx.resolver_destino(settings.comercial_mongodb_uri, settings.comercial_mongodb_db)
    assert cx.es_local(uri)
    assert base == settings.mongodb_db or base.endswith("_test")
    assert settings.comercial_mongodb_uri != settings.mongodb_uri or base.endswith("_test")


def test_validar_no_escribe_en_mongo(tmp_path, mongo_db):
    """`--validar` comprueba el contenido sin escribir una sola colección."""
    m5_fixture.escribir(tmp_path)
    informacion = ingesta.validar_archivos(tmp_path)
    assert informacion["validado"] is True
    assert informacion["contenido"]["series"] == 9
    assert informacion["contenido"]["suma_unidades"] == m5_fixture.total_esperado()
    assert informacion["contenido"]["dias_de_ventas"] == m5_fixture.DIAS
    assert informacion["contenido"]["dias_calendario_sin_ventas"] == 0
    for nombre in COLECCIONES_M5:
        assert mongo_db[nombre].count_documents({}) == 0


def test_validar_detecta_identificadores_duplicados(tmp_path):
    m5_fixture.escribir(tmp_path)
    ventas = tmp_path / "sales_train_evaluation.csv"
    lineas = ventas.read_text(encoding="utf-8").splitlines()
    lineas.append(lineas[1])  # repite la primera serie
    ventas.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    with pytest.raises(ingesta.ErrorImportacion, match="Identificadores repetidos"):
        ingesta.validar_archivos(tmp_path)


def test_reimportacion_no_destruye_version_activa(m5):
    """Reimportar la versión activa la reescribe sin dejarla incompleta."""
    db, version = m5["db"], m5["version"]
    antes = {
        nombre: db[nombre].count_documents({"version": version}) if nombre != M5_CALENDARIO else 0
        for nombre in COLECCIONES_M5
    }
    manifiesto = ingesta.importar(db, m5["carpeta"], forzar=True)
    assert manifiesto["version"] == version
    assert manifiesto["estado"] == "publicada"
    despues = {
        nombre: db[nombre].count_documents({"version": version}) if nombre != M5_CALENDARIO else 0
        for nombre in COLECCIONES_M5
    }
    assert despues == antes
    assert db["m5_estado"].find_one({"_id": "actual"})["version"] == version


def test_calendario_compartido_no_se_reescribe(m5):
    db = m5["db"]
    calendario = db[M5_CALENDARIO].count_documents({})
    ids = sorted(d["d"] for d in db[M5_CALENDARIO].find({}, {"d": 1}))
    manifiesto = ingesta.importar(db, m5["carpeta"], forzar=True)
    assert manifiesto["calendario"]["compartido"] is True
    assert db[M5_CALENDARIO].count_documents({}) == calendario
    assert sorted(d["d"] for d in db[M5_CALENDARIO].find({}, {"d": 1})) == ids


def test_cobertura_publicada_excluye_dias_de_calendario_sin_ventas(tmp_path, mongo_db):
    """La cobertura pública son los días CON ventas, no los del calendario."""
    for nombre in COLECCIONES_M5:
        mongo_db[nombre].delete_many({})
    mongo_db["m5_estado"].delete_many({})
    m5_fixture.escribir(tmp_path)
    calendario = tmp_path / "calendar.csv"
    lineas = calendario.read_text(encoding="utf-8").splitlines()
    cabecera, filas = lineas[0], lineas[1:]
    

    ultima = date.fromisoformat(filas[-1].split(",")[0])
    for extra_dia in range(1, 4):
        fecha = ultima + timedelta(days=extra_dia)
        dia = m5_fixture.DIAS + extra_dia
        filas.append(
            f"{fecha.isoformat()},11200,{fecha.strftime('%A')},{fecha.isoweekday()},"
            f"{fecha.month},{fecha.year},d_{dia},,,,,0,0,0"
        )
    calendario.write_text("\n".join([cabecera, *filas]) + "\n", encoding="utf-8")

    manifiesto = ingesta.importar(mongo_db, tmp_path)
    cobertura = consultas.cobertura_publicada(mongo_db, manifiesto["version"])
    assert cobertura["dias"] == m5_fixture.DIAS
    assert cobertura["hasta"] == ultima
    assert cobertura["dias_calendario_sin_ventas"] == 3


def test_baseline_con_calendario_mas_largo_que_ventas(tmp_path, mongo_db):
    """M5 trae 28 días de calendario sin ventas: el baseline no debe usarlos."""
    for nombre in COLECCIONES_M5:
        mongo_db[nombre].delete_many({})
    mongo_db["m5_estado"].delete_many({})
    m5_fixture.escribir(tmp_path)
    # Añadir días de calendario posteriores a la última venta (como M5: 1969 vs 1941).
    calendario = tmp_path / "calendar.csv"
    lineas = calendario.read_text(encoding="utf-8").splitlines()
    cabecera, filas = lineas[0], lineas[1:]
    

    ultima = date.fromisoformat(filas[-1].split(",")[0])
    for extra_dia in range(1, 6):
        fecha = ultima + timedelta(days=extra_dia)
        dia = m5_fixture.DIAS + extra_dia
        filas.append(f"{fecha.isoformat()},11200,{fecha.strftime('%A')},{fecha.isoweekday()},{fecha.month},{fecha.year},d_{dia},,,,,0,0,0")
    calendario.write_text("\n".join([cabecera, *filas]) + "\n", encoding="utf-8")

    manifiesto = ingesta.importar(mongo_db, tmp_path)
    assert manifiesto["calendario"]["dias_calendario_sin_ventas"] == 5

    resultado = forecast.calcular(mongo_db, manifiesto["version"], ventanas=2)
    assert resultado["pronosticos"] > 0
    for evaluacion in resultado["evaluaciones"]:
        assert evaluacion["resumen"]["n"] > 0
        assert evaluacion["resumen"]["wape"] is not None
    futuro = mongo_db[M5_PRONOSTICOS].find_one({"version": manifiesto["version"], "tipo": "futuro"})
    assert futuro["origen_d"] == m5_fixture.DIAS, "el origen debe ser el último día CON ventas"
    assert len(futuro["dias"]) == 28
