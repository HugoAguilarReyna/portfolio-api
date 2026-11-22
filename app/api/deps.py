"""Dependencias compartidas de la API.

Aquí se construyen los servicios y, sobre todo, se **normalizan los filtros en un
único sitio**: el resumen, el detalle y la exportación comparten exactamente la
misma validación y el mismo rango temporal.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.atlas.importer import estado_publico
from app.cnbv import conceptos as catalogo_conceptos
from app.cnbv import sources
from app.config import Settings, get_settings
from app.db.mongo import MongoDatabase, get_database
from app.models.atlas import AtlasFiltros, normalizar_lista
from app.models.marketing import MarketingFiltros
from app.models.walmex import WalmexFiltros, normalizar_lista as normalizar_mercados
from app.models.cnbv import CnbvConsulta, CnbvFiltros
from app.models.filters import DIMENSIONS, SORT_FIELDS, SalesFilters, SalesQuery
from app.repositories.atlas_repository import AtlasRepository
from app.repositories.comercial_repository import ComercialRepositorio, obtener_repositorio
from app.repositories.marketing_repository import MarketingRepository
from app.repositories.walmex_repository import WalmexRepository
from app.repositories.cnbv_repository import CnbvRepository
from app.repositories.dataset_repository import DatasetRepository
from app.repositories.sales_repository import SalesRepository
from app.services.catalog_service import CatalogService
from app.services.atlas_service import AtlasService
from app.services.comercial_service import ComercialServicio
from app.services.marketing_service import MarketingService
from app.services.walmex_service import WalmexService
from app.services.cnbv_service import CnbvService
from app.services.dashboard_service import DashboardService

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_db() -> MongoDatabase:
    return get_database()


DatabaseDep = Annotated[MongoDatabase, Depends(get_db)]


def get_sales_repository(db: DatabaseDep, settings: SettingsDep) -> SalesRepository:
    return SalesRepository(
        db.db,
        max_query_ms=settings.max_query_ms,
        max_options=settings.max_option_rows,
    )


SalesDep = Annotated[SalesRepository, Depends(get_sales_repository)]


def get_catalog_service(
    db: DatabaseDep, sales: SalesDep, settings: SettingsDep
) -> CatalogService:
    return CatalogService(DatasetRepository(db.db), sales)


CatalogDep = Annotated[CatalogService, Depends(get_catalog_service)]


def get_dashboard_service(
    sales: SalesDep, catalog: CatalogDep, settings: SettingsDep
) -> DashboardService:
    return DashboardService(sales, catalog, settings)


DashboardDep = Annotated[DashboardService, Depends(get_dashboard_service)]


# --------------------------------------------------------------------- filtros
def _error_validacion(exc: ValidationError) -> RequestValidationError:
    return RequestValidationError(exc.errors())


def resolve_sales_filters(
    catalog: CatalogDep,
    settings: SettingsDep,
    dataset_id: Annotated[
        str | None,
        Query(description="Identificador del dataset. Si solo hay uno, es opcional."),
    ] = None,
    date_from: Annotated[
        date | None, Query(description="Desde (AAAA-MM-DD), día natural UTC, inclusivo.")
    ] = None,
    date_to: Annotated[
        date | None, Query(description="Hasta (AAAA-MM-DD), día natural UTC, inclusivo.")
    ] = None,
    regions: Annotated[
        list[str] | None, Query(alias="region", description="Región; repetir para varias.")
    ] = None,
    channels: Annotated[
        list[str] | None, Query(alias="channel", description="Canal; repetir para varios.")
    ] = None,
    categories: Annotated[
        list[str] | None, Query(alias="category", description="Categoría; repetir para varias.")
    ] = None,
    products: Annotated[
        list[str] | None, Query(alias="product", description="Producto; repetir para varios.")
    ] = None,
    customers: Annotated[
        list[str] | None, Query(alias="customer", description="Cliente; repetir para varios.")
    ] = None,
) -> SalesFilters:
    """Valida y normaliza los filtros de cualquier consulta del dashboard."""
    dataset = (dataset_id or "").strip().lower()
    if not dataset:
        dataset = catalog.default_dataset_id()
        if dataset is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "dataset_id es obligatorio: este entorno tiene varios datasets. "
                    "Consulta GET /api/v1/datasets para verlos."
                ),
            )

    meta = catalog.metadata_or_404(dataset)
    desde_ds, hasta_ds = catalog.period_of(meta)
    rango = f"{desde_ds.isoformat()} a {hasta_ds.isoformat()}"

    if date_from is None and date_to is None:
        desde = max(desde_ds, hasta_ds - timedelta(days=settings.default_range_days - 1))
        hasta = hasta_ds
    elif date_from is None:
        desde, hasta = desde_ds, date_to
    elif date_to is None:
        desde, hasta = date_from, hasta_ds
    else:
        desde, hasta = date_from, date_to

    if desde > hasta:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="date_from no puede ser posterior a date_to.",
        )
    if desde < desde_ds or hasta > hasta_ds:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Las fechas deben estar dentro del periodo cubierto por el dataset "
                f"({rango})."
            ),
        )
    if (hasta - desde).days + 1 > settings.max_date_range_days:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"El rango no puede superar {settings.max_date_range_days} días. "
                "Acota las fechas o filtra por otra dimensión."
            ),
        )

    seleccion = {
        "regions": regions or (),
        "channels": channels or (),
        "categories": categories or (),
        "products": products or (),
        "customers": customers or (),
    }
    for dimension, valores in seleccion.items():
        if len(valores) > settings.max_filter_values:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"'{dimension}' admite como máximo {settings.max_filter_values} valores "
                    f"(recibidos {len(valores)})."
                ),
            )

    try:
        return SalesFilters(
            dataset_id=dataset,
            date_from=desde,
            date_to=hasta,
            **seleccion,
        )
    except ValidationError as exc:  # valores con formato no válido
        raise _error_validacion(exc) from exc


FiltersDep = Annotated[SalesFilters, Depends(resolve_sales_filters)]


def resolve_sales_query(
    settings: SettingsDep,
    page: Annotated[int, Query(ge=1, description="Página, empezando en 1.")] = 1,
    page_size: Annotated[int, Query(ge=1, description="Tamaño de página.")] = 50,
    sort: Annotated[
        str, Query(description="Campo de orden: " + ", ".join(sorted(SORT_FIELDS)))
    ] = "fecha",
    order: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
) -> SalesQuery:
    if page_size > settings.max_page_size:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"page_size no puede superar {settings.max_page_size}.",
        )
    try:
        return SalesQuery(page=page, page_size=page_size, sort=sort, order=order)
    except ValidationError as exc:
        raise _error_validacion(exc) from exc


QueryDep = Annotated[SalesQuery, Depends(resolve_sales_query)]

# Reexportado para documentar las dimensiones válidas en una sola fuente.
DIMENSIONES = DIMENSIONS


# ------------------------------------------------------- CNBV (datos reales)
def get_cnbv_repository(db: DatabaseDep, settings: SettingsDep) -> CnbvRepository:
    return CnbvRepository(db.db, max_query_ms=settings.max_query_ms)


CnbvRepoDep = Annotated[CnbvRepository, Depends(get_cnbv_repository)]


def get_cnbv_service(repo: CnbvRepoDep, settings: SettingsDep) -> CnbvService:
    return CnbvService(repo, settings)


CnbvServiceDep = Annotated[CnbvService, Depends(get_cnbv_service)]


def resolve_cnbv_filters(
    repo: CnbvRepoDep,
    settings: SettingsDep,
    periodo: Annotated[
        str | None,
        Query(
            description=(
                "Periodo de referencia del boletín (AAAA-MM). Si se omite, se usa el "
                "más reciente importado."
            )
        ),
    ] = None,
    concepto: Annotated[
        str | None,
        Query(
            description=(
                "Identificador del concepto financiero (ver /catalogo). Por omisión, "
                "el activo total."
            )
        ),
    ] = None,
    instituciones: Annotated[
        list[str] | None,
        Query(alias="institucion", description="Institución; repetir para varias."),
    ] = None,
    incluir_sistema: Annotated[
        bool, Query(description="Incluir la fila del agregado del sector (Sistema).")
    ] = True,
    dataset_id: str = sources.DATASET_ID,
) -> CnbvFiltros:
    """Normaliza los filtros del comparativo bancario (datos reales)."""
    objetivo_periodo = periodo
    if not objetivo_periodo:
        disponibles = repo.periodos(dataset_id)
        if not disponibles:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "No hay boletines de la CNBV importados. Ejecuta "
                    "`python -m scripts.importar_cnbv --descargar`."
                ),
            )
        objetivo_periodo = disponibles[-1]

    objetivo_concepto = concepto or catalogo_conceptos.CONCEPTO_POR_OMISION

    if instituciones and len(instituciones) > settings.max_filter_values:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"'institucion' admite como máximo {settings.max_filter_values} valores "
                f"(recibidos {len(instituciones)})."
            ),
        )

    try:
        return CnbvFiltros(
            dataset_id=dataset_id,
            periodo=objetivo_periodo,
            concepto=objetivo_concepto,
            instituciones=instituciones or (),
            incluir_sistema=incluir_sistema,
        )
    except ValidationError as exc:
        raise _error_validacion(exc) from exc


CnbvFiltrosDep = Annotated[CnbvFiltros, Depends(resolve_cnbv_filters)]


def resolve_cnbv_query(
    settings: SettingsDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1)] = 50,
    orden: Annotated[str, Query(pattern="^(valor|institucion|concepto|periodo)$")] = "valor",
) -> CnbvConsulta:
    if page_size > settings.max_page_size:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"page_size no puede superar {settings.max_page_size}.",
        )
    try:
        return CnbvConsulta(page=page, page_size=page_size, orden=orden)
    except ValidationError as exc:
        raise _error_validacion(exc) from exc


CnbvQueryDep = Annotated[CnbvConsulta, Depends(resolve_cnbv_query)]

# --------------------------------------------------------------- Atlas vial
def get_atlas_repository(db: DatabaseDep, settings: SettingsDep) -> AtlasRepository:
    return AtlasRepository(db.db, max_query_ms=settings.max_query_ms)


AtlasRepoDep = Annotated[AtlasRepository, Depends(get_atlas_repository)]


def get_atlas_service(repo: AtlasRepoDep, settings: SettingsDep) -> AtlasService:
    return AtlasService(repo, settings)


AtlasServiceDep = Annotated[AtlasService, Depends(get_atlas_service)]


def resolve_atlas_filtros(
    settings: SettingsDep,
    desde: Annotated[
        date | None, Query(description="Primer día del periodo (AAAA-MM-DD), inclusivo.")
    ] = None,
    hasta: Annotated[
        date | None, Query(description="Último día del periodo (AAAA-MM-DD), inclusivo.")
    ] = None,
    alcaldias: Annotated[
        list[str] | None, Query(alias="alcaldia", description="Alcaldía; repetir para varias.")
    ] = None,
    tipos_evento: Annotated[
        list[str] | None, Query(alias="tipo_evento", description="Tipo de evento (forma canónica).")
    ] = None,
    consecuencias: Annotated[
        list[str] | None,
        Query(
            alias="consecuencia",
            description="con_fallecimientos | solo_lesionados",
        ),
    ] = None,
    tipos_usuario: Annotated[
        list[str] | None,
        Query(alias="tipo_usuario", description="Tipo de persona registrado en el hecho."),
    ] = None,
    tipos_vehiculo: Annotated[
        list[str] | None, Query(alias="tipo_vehiculo", description="Tipo de vehículo registrado.")
    ] = None,
    vialidades: Annotated[
        list[str] | None, Query(alias="vialidad", description="Clasificación de la vialidad.")
    ] = None,
    semaforizada: Annotated[
        list[str] | None, Query(description="si | no | sin_dato")
    ] = None,
    sexos: Annotated[
        list[str] | None, Query(alias="sexo", description="Sexo registrado en el hecho.")
    ] = None,
    edad_min: Annotated[int | None, Query(ge=0, le=120)] = None,
    edad_max: Annotated[int | None, Query(ge=0, le=120)] = None,
    buscar: Annotated[
        str,
        Query(
            max_length=80,
            description="Búsqueda por folio o texto de ubicación (colonia o vialidad).",
        ),
    ] = "",
    bbox: Annotated[
        str | None,
        Query(
            description=(
                "Extensión visible como lon_min,lat_min,lon_max,lat_max. Solo se aplica "
                "si filtrar_por_zona=true."
            )
        ),
    ] = None,
    filtrar_por_zona: Annotated[
        bool,
        Query(description="Aplica el recorte por la zona visible del mapa (por omisión no)."),
    ] = False,
) -> AtlasFiltros:
    """Normaliza los filtros compartidos por todas las vistas del Atlas vial."""
    if desde and hasta and desde > hasta:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="La fecha inicial no puede ser posterior a la final.",
        )
    if edad_min is not None and edad_max is not None and edad_min > edad_max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="La edad mínima no puede superar a la máxima.",
        )

    caja = None
    if bbox:
        partes = bbox.split(",")
        if len(partes) != 4:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="bbox debe tener cuatro valores: lon_min,lat_min,lon_max,lat_max.",
            )
        try:
            valores = tuple(float(parte) for parte in partes)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="bbox contiene valores que no son números.",
            ) from exc
        if valores[0] >= valores[2] or valores[1] >= valores[3]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="bbox tiene los extremos invertidos.",
            )
        caja = valores  # type: ignore[assignment]

    seleccion = {
        "alcaldias": normalizar_lista(alcaldias),
        "tipos_evento": normalizar_lista(tipos_evento),
        "consecuencias": normalizar_lista(consecuencias),
        "tipos_usuario": normalizar_lista(tipos_usuario),
        "tipos_vehiculo": normalizar_lista(tipos_vehiculo),
        "vialidades": normalizar_lista(vialidades),
        "semaforizada": normalizar_lista(semaforizada),
        "sexos": normalizar_lista(sexos),
    }
    for dimension, valores in seleccion.items():
        if len(valores) > settings.max_filter_values:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"'{dimension}' admite como máximo {settings.max_filter_values} valores "
                    f"(recibidos {len(valores)})."
                ),
            )

    return AtlasFiltros(
        desde=desde,
        hasta=hasta,
        bbox=caja,
        filtrar_por_zona=filtrar_por_zona and caja is not None,
        edad_min=edad_min,
        edad_max=edad_max,
        buscar=(buscar or "").strip(),
        **seleccion,
    )


AtlasFiltrosDep = Annotated[AtlasFiltros, Depends(resolve_atlas_filtros)]

# ------------------------------------------------- Marketing y clientes (GA4)
def get_marketing_repository(db: DatabaseDep, settings: SettingsDep) -> MarketingRepository:
    return MarketingRepository(db.marketing_db, max_query_ms=settings.max_query_ms)


MarketingRepoDep = Annotated[MarketingRepository, Depends(get_marketing_repository)]


def get_marketing_service(repo: MarketingRepoDep, settings: SettingsDep) -> MarketingService:
    return MarketingService(repo, settings)


MarketingServiceDep = Annotated[MarketingService, Depends(get_marketing_service)]


def resolve_marketing_filtros(
    settings: SettingsDep,
    desde: Annotated[
        date | None, Query(description="Primer día del rango (AAAA-MM-DD), inclusivo.")
    ] = None,
    hasta: Annotated[
        date | None, Query(description="Último día del rango (AAAA-MM-DD), inclusivo.")
    ] = None,
    canales: Annotated[
        list[str] | None,
        Query(alias="canal", description="Canal «fuente / medio»; repetir para varios."),
    ] = None,
    dispositivos: Annotated[
        list[str] | None,
        Query(alias="dispositivo", description="Dispositivo; repetir para varios."),
    ] = None,
    paises: Annotated[
        list[str] | None, Query(alias="pais", description="País; repetir para varios.")
    ] = None,
    buscar: Annotated[
        str,
        Query(
            max_length=80,
            description=(
                "Reservado para el extracto de eventos en tabla. El modelo compacto no "
                "guarda eventos uno a uno, así que hoy no cambia ninguna cifra."
            ),
        ),
    ] = "",
) -> MarketingFiltros:
    """Normaliza los filtros compartidos por todas las vistas de marketing."""
    if desde and hasta and desde > hasta:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="La fecha inicial no puede ser posterior a la final.",
        )

    seleccion = {
        "canales": normalizar_lista(canales),
        "dispositivos": normalizar_lista(dispositivos),
        "paises": normalizar_lista(paises),
    }
    for dimension, valores in seleccion.items():
        if len(valores) > settings.max_filter_values:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"'{dimension}' admite como máximo {settings.max_filter_values} valores "
                    f"(recibidos {len(valores)})."
                ),
            )

    return MarketingFiltros(
        desde=desde,
        hasta=hasta,
        busqueda=(buscar or "").strip(),
        **seleccion,
    )


MarketingFiltrosDep = Annotated[MarketingFiltros, Depends(resolve_marketing_filtros)]


# ------------------------------------------------ Pulso comercial · Walmex
def get_walmex_repository(db: DatabaseDep, settings: SettingsDep) -> WalmexRepository:
    return WalmexRepository(db.db, max_query_ms=settings.max_query_ms)


WalmexRepoDep = Annotated[WalmexRepository, Depends(get_walmex_repository)]


def get_walmex_service(repo: WalmexRepoDep, settings: SettingsDep) -> WalmexService:
    return WalmexService(repo, settings)


WalmexServiceDep = Annotated[WalmexService, Depends(get_walmex_service)]


def resolve_walmex_filtros(
    settings: SettingsDep,
    mercados: Annotated[
        list[str] | None,
        Query(alias="mercado", description="Mercado publicado; repetir para varios."),
    ] = None,
    desde: Annotated[
        str | None,
        Query(pattern="^[1-4]T\\d{2}$", description="Primer trimestre del rango (por ejemplo 1T24)."),
    ] = None,
    hasta: Annotated[
        str | None,
        Query(pattern="^[1-4]T\\d{2}$", description="Último trimestre del rango (por ejemplo 3T26)."),
    ] = None,
    indicador: Annotated[
        str | None,
        Query(
            pattern="^(crecimiento_anual|mismas_tiendas|trafico|ticket|total_ingresos)$",
            description="Indicador para la matriz de desempeño.",
        ),
    ] = None,
    base: Annotated[
        str | None,
        Query(
            pattern="^(nominal|nominal_publicado|nominal_calculado|moneda_constante_publicado|"
            "unidades_iguales_publicado|publicado_comunicado)$",
            description="Base de comparación publicada para el indicador.",
        ),
    ] = None,
    indicadores: Annotated[
        list[str] | None,
        Query(
            alias="indicadores",
            description=(
                "Uno o varios indicadores de variación para /walmex/matriz; separados por "
                "coma o repetidos. Por omisión: crecimiento anual, unidades iguales, tráfico "
                "y ticket."
            ),
        ),
    ] = None,
) -> WalmexFiltros:
    """Normaliza los filtros del tablero de Walmex."""
    seleccion = normalizar_mercados(mercados)
    if len(seleccion) > settings.max_filter_values:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"'mercado' admite como máximo {settings.max_filter_values} valores "
                f"(recibidos {len(seleccion)})."
            ),
        )
    pedidos: list[str] = []
    for valor in indicadores or []:
        pedidos.extend(parte.strip() for parte in valor.split(",") if parte.strip())
    pedidos = normalizar_mercados(pedidos)
    if len(pedidos) > settings.max_filter_values:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"'indicadores' admite como máximo {settings.max_filter_values} valores "
                f"(recibidos {len(pedidos)})."
            ),
        )
    return WalmexFiltros(
        mercados=seleccion,
        desde=desde,
        hasta=hasta,
        indicador=indicador,
        base=base,
        indicadores=pedidos,
    )


WalmexFiltrosDep = Annotated[WalmexFiltros, Depends(resolve_walmex_filtros)]

# --- Atlas comercial · Demanda y ventas (M5) ---------------------------------
def get_comercial_repositorio(db: DatabaseDep, settings: SettingsDep) -> ComercialRepositorio:
    """Repositorio compartido por proceso: la caché sirve entre peticiones."""
    return obtener_repositorio(db.m5_db, cache_ttl_s=settings.comercial_cache_ttl_s)


ComercialRepositorioDep = Annotated[ComercialRepositorio, Depends(get_comercial_repositorio)]


def get_comercial_servicio(repositorio: ComercialRepositorioDep) -> ComercialServicio:
    return ComercialServicio(repositorio)


ComercialServicioDep = Annotated[ComercialServicio, Depends(get_comercial_servicio)]
