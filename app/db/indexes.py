"""Índices de MongoDB.

Criterio: **un índice compuesto por dataset y fecha** cubre todas las consultas
implementadas, porque todas filtran por `dataset_id` y por un rango de `fecha`.

Los filtros de región, canal, categoría, producto y cliente se resuelven como
filtros residuales sobre el rango de fechas seleccionado, que en el dataset
demostrativo son unas decenas de miles de documentos: un índice por cada
dimensión sería sobreindexar (más escritura, más memoria y ningún beneficio
medible). Si en el futuro un dataset crece de forma que un filtro de dimensión
sea más selectivo que la fecha, se añadirá entonces, con su `explain`.

Este criterio se comprueba en las pruebas: la consulta representativa debe usar
IXSCAN y no COLLSCAN.
"""

from __future__ import annotations

import logging

from pymongo import ASCENDING
from pymongo.database import Database

from app.db.mongo import (
    ATLAS_BLOQUEOS,
    ATLAS_HECHOS,
    ATLAS_IMPORTACIONES,
    CNBV_BOLETINES,
    CNBV_HECHOS,
    CNBV_INDICADORES,
    DATASETS_COLLECTION,
    M5_AGREGADOS,
    M5_BLOQUES,
    M5_CALENDARIO,
    M5_CATALOGO,
    M5_EVALUACION,
    M5_EVALUACION_MODELOS,
    M5_IMPORTACIONES,
    M5_MODELOS,
    M5_PRECIOS,
    M5_PRECIOS_MANIFEST,
    M5_PREDICCIONES,
    M5_PREDICCIONES_SERIES,
    M5_PRONOSTICOS,
    MARKETING_BLOQUEOS,
    MARKETING_COHORTES,
    MARKETING_COMPRAS,
    MARKETING_EMBUDO,
    MARKETING_IMPORTACIONES,
    MARKETING_RESUMEN,
    MARKETING_USUARIOS,
    METRICAS_VISITAS,
    METRICAS_VISITAS_LIMITES,
)

logger = logging.getLogger(__name__)

# (colección, nombre del índice, claves, opciones)
#: Solo se declaran índices de colecciones que se publican. ``sales`` (el dataset
#: sintético de «Ventas / Rentabilidad») se archivó fuera del clúster: declarar su
#: índice volvería a crear la colección vacía al arrancar la API.
INDEX_SPECS: list[tuple[str, str, list[tuple[str, int]], dict]] = [
    (
        DATASETS_COLLECTION,
        "datasets_dataset_id",
        [("dataset_id", ASCENDING)],
        {"unique": True},
    ),
    # --- CNBV ---------------------------------------------------------------
    # Clave natural de un hecho: una sola fila viva por periodo/institución/
    # concepto y revisión (la unicidad la garantiza el upsert, no el índice).
    # El índice por periodo+concepto se retiró: toda consulta de un periodo y un
    # concepto la cubren los dos índices siguientes.
    # Serie temporal de una institución y un concepto.
    (
        CNBV_HECHOS,
        "cnbv_hechos_concepto_institucion_periodo",
        [
            ("dataset_id", ASCENDING),
            ("concepto_id", ASCENDING),
            ("institucion_id", ASCENDING),
            ("periodo", ASCENDING),
        ],
        {},
    ),
    # Detalle paginado y catálogos por periodo.
    (
        CNBV_HECHOS,
        "cnbv_hechos_periodo",
        [("dataset_id", ASCENDING), ("periodo", ASCENDING), ("seccion", ASCENDING)],
        {},
    ),
    (
        CNBV_BOLETINES,
        "cnbv_boletines_periodo_revision",
        [
            ("dataset_id", ASCENDING),
            ("periodo", ASCENDING),
            ("revision", ASCENDING),
        ],
        {"unique": True},
    ),
    (
        CNBV_BOLETINES,
        "cnbv_boletines_vigente",
        [("dataset_id", ASCENDING), ("vigente", ASCENDING), ("periodo", ASCENDING)],
        {},
    ),
    (
        CNBV_INDICADORES,
        "cnbv_indicadores_periodo_indicador",
        [
            ("dataset_id", ASCENDING),
            ("periodo", ASCENDING),
            ("indicador_id", ASCENDING),
            ("institucion_id", ASCENDING),
        ],
        {},
    ),
    # Los indicadores se consultan por boletín (un archivo trae tres fechas).
    (
        CNBV_INDICADORES,
        "cnbv_indicadores_boletin",
        [
            ("dataset_id", ASCENDING),
            ("periodo_boletin", ASCENDING),
            ("indicador_id", ASCENDING),
            ("institucion_id", ASCENDING),
        ],
        {},
    ),
    # --- Atlas vial ---------------------------------------------------------
    # Clave natural de un hecho: versión + folio (el folio no es único entre
    # versiones del conjunto, así que la versión forma parte de la clave).
    (
        ATLAS_HECHOS,
        "atlas_hechos_version_folio",
        [("version", ASCENDING), ("folio", ASCENDING)],
        {"unique": True},
    ),
    # Orden estable de puntos, tabla y exportación (sirve también los rangos de
    # fecha: es el prefijo version+fecha). Con este índice no hacen falta los de
    # fecha sola, alcaldía ni tipo: filtrar sobre 30 000 puntos con él es más
    # barato que mantener tres índices más en un clúster de 512 MB.
    (
        ATLAS_HECHOS,
        "atlas_hechos_version_fecha_hora_folio",
        [
            ("version", ASCENDING),
            ("fecha", ASCENDING),
            ("hora", ASCENDING),
            ("folio", ASCENDING),
        ],
        {},
    ),
    # Consultas de extensión visible y mapa de puntos (índice geoespacial).
    (
        ATLAS_HECHOS,
        "atlas_hechos_version_ubicacion",
        [("version", ASCENDING), ("ubicacion", "2dsphere")],
        {},
    ),
    (
        ATLAS_IMPORTACIONES,
        "atlas_importaciones_version",
        [("version", ASCENDING)],
        {"unique": True},
    ),
    (
        ATLAS_BLOQUEOS,
        "atlas_bloqueos_nombre",
        [("nombre", ASCENDING)],
        {"unique": True},
    ),
    # --- Marketing y clientes (GA4 · BigQuery) -------------------------------
    (
        MARKETING_RESUMEN,
        "marketing_resumen_version_fecha",
        [
            ("version", ASCENDING),
            ("fecha", ASCENDING),
            ("dispositivo", ASCENDING),
            ("pais", ASCENDING),
            ("fuente", ASCENDING),
            ("medio", ASCENDING),
        ],
        {},
    ),
    (
        MARKETING_USUARIOS,
        "marketing_usuarios_version_fecha_usuario",
        [("version", ASCENDING), ("fecha", ASCENDING), ("usuario", ASCENDING)],
        {},
    ),
    # Conteo exacto de usuarios distintos por canal (cubre la consulta con índice:
    # no hay que leer los documentos completos). Dispositivo, país y compra se
    # resuelven sobre una colección de ~15 000 documentos, así que no se indexan:
    # el índice costaba más espacio del que ahorraba.
    (
        MARKETING_USUARIOS,
        "marketing_usuarios_version_canal_usuario",
        [("version", ASCENDING), ("canal", ASCENDING), ("usuario", ASCENDING)],
        {},
    ),
    (
        MARKETING_RESUMEN,
        "marketing_resumen_version_canal_fecha",
        [("version", ASCENDING), ("canal", ASCENDING), ("fecha", ASCENDING)],
        {},
    ),
    (
        MARKETING_RESUMEN,
        "marketing_resumen_version_dispositivo",
        [("version", ASCENDING), ("dispositivo", ASCENDING)],
        {},
    ),
    (
        MARKETING_RESUMEN,
        "marketing_resumen_version_pais",
        [("version", ASCENDING), ("pais", ASCENDING)],
        {},
    ),
    (
        MARKETING_COMPRAS,
        "marketing_compras_version_fecha",
        [("version", ASCENDING), ("fecha", ASCENDING), ("canal", ASCENDING)],
        {},
    ),
    (
        MARKETING_COMPRAS,
        "marketing_compras_version_usuario",
        [("version", ASCENDING), ("usuario", ASCENDING)],
        {},
    ),
    # El modelo compacto no guarda sesiones, artículos ni eventos: sus índices
    # se retiraron para no recrear colecciones vacías al arrancar la API.
    # Categorías queda en ~500 filas (una por compra y categoría), así que tampoco
    # necesita índice: leerla completa es más barato que mantenerlo.
    (
        MARKETING_EMBUDO,
        "marketing_embudo_version_paso",
        [("version", ASCENDING), ("paso", ASCENDING), ("fecha", ASCENDING)],
        {},
    ),
    (
        MARKETING_EMBUDO,
        "marketing_embudo_version_canal_paso",
        [("version", ASCENDING), ("canal", ASCENDING), ("paso", ASCENDING)],
        {},
    ),
    (
        MARKETING_COHORTES,
        "marketing_cohortes_version_dimension",
        [
            ("version", ASCENDING),
            ("dimension", ASCENDING),
            ("valor", ASCENDING),
            ("cohorte", ASCENDING),
            ("indice", ASCENDING),
        ],
        {},
    ),
    (
        MARKETING_IMPORTACIONES,
        "marketing_importaciones_version",
        [("version", ASCENDING)],
        {"unique": True},
    ),
    (
        MARKETING_BLOQUEOS,
        "marketing_bloqueos_nombre",
        [("nombre", ASCENDING)],
        {"unique": True},
    ),
    # --- Atlas comercial · M5 -------------------------------------------------
    (
        M5_AGREGADOS,
        "m5_agregados_version_dimension_clave",
        [("version", ASCENDING), ("dimension", ASCENDING), ("clave", ASCENDING)],
        {"unique": True},
    ),
    (
        M5_BLOQUES,
        "m5_bloques_version_store_item",
        [("version", ASCENDING), ("store_id", ASCENDING), ("item_id", ASCENDING)],
        {},
    ),
    (
        M5_CATALOGO,
        "m5_catalogo_version_tipo",
        [("version", ASCENDING), ("tipo", ASCENDING)],
        {},
    ),
    (
        M5_CATALOGO,
        "m5_catalogo_version_item",
        [("version", ASCENDING), ("item_id", ASCENDING)],
        {},
    ),
    (
        M5_PRONOSTICOS,
        "m5_pronosticos_version_nivel_tipo",
        [("version", ASCENDING), ("nivel", ASCENDING), ("tipo", ASCENDING)],
        {},
    ),
    (
        M5_EVALUACION,
        "m5_evaluacion_version_nivel",
        [("version", ASCENDING), ("nivel", ASCENDING)],
        {"unique": True},
    ),
    (M5_CALENDARIO, "m5_calendario_d", [("d", ASCENDING)], {"unique": True}),
    (M5_CALENDARIO, "m5_calendario_fecha", [("fecha", ASCENDING)], {}),
    (M5_IMPORTACIONES, "m5_importaciones_estado", [("estado", ASCENDING)], {}),
    # --- Modelo predictivo y precios -----------------------------------------
    # Precios: una lectura por versión y, para el modelo, por tienda y semana.
    (
        M5_PRECIOS,
        "m5_precios_version_tienda_semana",
        [("version_precios", ASCENDING), ("store_id", ASCENDING), ("wm_yr_wk", ASCENDING)],
        {"unique": True},
    ),
    (M5_PRECIOS_MANIFEST, "m5_precios_manifest_importado", [("importado_en", ASCENDING)], {}),
    # Ejecuciones: la más reciente y publicada se busca por versión y estado.
    (
        M5_MODELOS,
        "m5_modelos_version_estado",
        [("version", ASCENDING), ("estado", ASCENDING), ("creado_en", ASCENDING)],
        {},
    ),
    # Predicciones agregadas: consulta por ejecución, origen y dimensión.
    (
        M5_PREDICCIONES,
        "m5_predicciones_run_origen_dimension",
        [
            ("run_id", ASCENDING),
            ("origen_d", ASCENDING),
            ("dimension", ASCENDING),
            ("clave", ASCENDING),
        ],
        {"unique": True},
    ),
    # Predicciones artículo × tienda publicadas (una lectura por serie).
    (
        M5_PREDICCIONES_SERIES,
        "m5_predicciones_series_run_store_item",
        [("run_id", ASCENDING), ("store_id", ASCENDING), ("item_id", ASCENDING)],
        {"unique": True},
    ),
    (
        M5_EVALUACION_MODELOS,
        "m5_evaluacion_modelos_run_origen_nivel",
        [
            ("run_id", ASCENDING),
            ("origen_d", ASCENDING),
            ("nivel", ASCENDING),
            ("clave", ASCENDING),
        ],
        {},
    ),
    # --- Telemetría anónima -------------------------------------------------
    # Las huellas diarias se borran solas a las 48 h. El documento `_id: global`
    # no tiene `expira_en`, así que el TTL nunca lo elimina.
    (
        METRICAS_VISITAS,
        "metricas_visitas_ttl",
        [("expira_en", ASCENDING)],
        {"expireAfterSeconds": 0},
    ),
    # Contadores de frecuencia: caducan solos (TTL de 2 h) y guardan solo un
    # contador por huella y minuto, nunca la IP.
    (
        METRICAS_VISITAS_LIMITES,
        "metricas_visitas_limites_ttl",
        [("expira_en", ASCENDING)],
        {"expireAfterSeconds": 0},
    ),
]


def ensure_indexes(db: Database, solo: tuple[str, ...] = ()) -> list[str]:
    """Crea los índices que falten. Es idempotente.

    ``solo`` limita el trabajo a esas colecciones: al arrancar, los índices del
    proyecto M5 se crean en **su** base y no deben arrastrar los de otros proyectos
    (crearían colecciones vacías ajenas en una base específica del proyecto).
    """
    created: list[str] = []
    for collection_name, name, keys, options in INDEX_SPECS:
        if solo and collection_name not in solo:
            continue
        collection = db[collection_name]
        existing = {index["name"] for index in collection.list_indexes()}
        if name in existing:
            continue
        collection.create_index(keys, name=name, **options)
        created.append(name)
        logger.info("Índice creado: %s.%s", collection_name, name)
    return created

