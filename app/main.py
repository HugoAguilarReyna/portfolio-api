"""Punto de entrada de la API.

Arranque:

    uvicorn app.main:app --reload --port 8000

Documentación interactiva: ``/api/docs`` (OpenAPI en ``/api/openapi.json``).
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from app.api.v1.router import api_router
from app.routers import visitas
from app.config import Settings, get_settings
from app.db.indexes import ensure_indexes
from app.db.mongo import close_database, init_database

logger = logging.getLogger("app")


class IPVisitanteMiddleware(BaseHTTPMiddleware):
    """Deja la IP real del visitante en el contexto de la petición.

    La resolución (y el hecho de confiar o no en `X-Forwarded-For`) vive en
    `app.llm.limites.ip_real`; aquí solo se propaga. La IP no se registra ni se
    persiste: el almacén de cuotas guarda un HMAC con vencimiento diario.
    """

    def __init__(self, app, settings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request, call_next):  # type: ignore[override]
        from app.llm import contexto, limites

        host = request.client.host if request.client else None
        token = contexto.fijar_ip(limites.ip_real(host, request.headers, self.settings))
        try:
            return await call_next(request)
        finally:
            contexto.restablecer(token)


class LimiteCuerpoMiddleware(BaseHTTPMiddleware):
    """Rechaza cuerpos mayores que el tope configurado (413).

    Los límites de longitud de los parámetros no cubren un cuerpo POST grande:
    este middleware lo corta antes de leerlo, sin abrir la puerta a cargas
    arbitrarias. El valor se configura en `HTTP_MAX_CUERPO_BYTES`.
    """

    def __init__(self, app, maximo: int) -> None:
        super().__init__(app)
        self.maximo = int(maximo)

    async def dispatch(self, request, call_next):  # type: ignore[override]
        declarado = request.headers.get("content-length")
        if declarado and declarado.isdigit() and int(declarado) > self.maximo:
            return JSONResponse(
                {"detail": "El cuerpo de la petición es demasiado grande."},
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        return await call_next(request)


def _precalentar(settings: Settings, db) -> None:
    """Calcula en segundo plano las vistas por omisión y deja la caché lista.

    No bloquea el arranque: si falla, el primer visitante simplemente paga el
    coste de la primera consulta.
    """
    import threading

    def trabajo() -> None:
        try:
            from app.models.marketing import MarketingFiltros
            from app.repositories.marketing_repository import MarketingRepository
            from app.services.marketing_service import MarketingService

            servicio = MarketingService(
                MarketingRepository(db.db, settings.max_query_ms), settings
            )
            if not servicio._disponible():
                return
            filtros = MarketingFiltros()
            for endpoint, calcular in (
                ("resumen", lambda: servicio.resumen(filtros)),
                ("adquisicion", lambda: servicio.adquisicion(filtros)),
                ("conversion", lambda: servicio.conversion(filtros)),
                ("cohortes", lambda: servicio.cohortes(filtros, "canal")),
                ("productos", lambda: servicio.productos(filtros)),
            ):
                inicio = time.perf_counter()
                servicio.desde_cache(endpoint, filtros, calcular, extra="canal" if endpoint == "cohortes" else "")
                logger.info(
                    "Caché precargada: %s en %.1f s", endpoint, time.perf_counter() - inicio
                )
        except Exception:  # noqa: BLE001 - el precalentamiento nunca tumba la API
            logger.warning("No se pudo precalentar la caché de marketing", exc_info=True)

    threading.Thread(target=trabajo, name="precalentar-marketing", daemon=True).start()

    def trabajo_comercial() -> None:
        """Deja calculada la vista por omisión de «Pulso de ventas».

        La concentración por producto recorre todos los bloques de la versión
        (304 900 en M5): se calcula una vez al arrancar para que el primer visitante
        no espere. Si falla, el tablero la calcula al pedirla.
        """
        try:
            from app.comercial import consultas
            from app.comercial.ingesta import version_actual
            from app.repositories.comercial_repository import obtener_repositorio

            version = version_actual(db.m5_db)
            if not version:
                return
            repositorio = obtener_repositorio(
                db.m5_db, cache_ttl_s=settings.comercial_cache_ttl_s
            )
            filtros = consultas.resolver_filtros(db.m5_db, version, {})
            inicio = time.perf_counter()
            repositorio.pulso(version, filtros, "semana")
            logger.info("Caché precargada: pulso comercial en %.1f s", time.perf_counter() - inicio)
        except Exception:  # noqa: BLE001 - el precalentamiento nunca tumba la API
            logger.warning("No se pudo precalentar la caché comercial", exc_info=True)

    threading.Thread(target=trabajo_comercial, name="precalentar-comercial", daemon=True).start()



@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    # Falla cerrado si el tope diario no admite ni un run del agente.
    from app.llm.configuracion import validar_arranque

    validar_arranque(settings, logger)
    # Catálogo cerrado de la telemetría: se valida al arrancar y se cachea. Si
    # falta o no cumple el esquema, las rutas de escritura responden 503.
    try:
        from app.services.catalogo_ids import catalogo_ids

        catalogo = catalogo_ids()
        logger.info(
            "Catálogo de telemetría cargado: %s proyectos y %s áreas",
            len(catalogo.proyectos),
            len(catalogo.areas),
        )
    except Exception:  # noqa: BLE001 - la API sigue sirviendo; telemetría falla cerrada
        logger.error(
            "No se pudo cargar app/data/catalogo_ids.json: la telemetría de "
            "proyectos quedará en 503 hasta sincronizar el catálogo",
            exc_info=True,
        )
    db = init_database(settings)
    app.state.database = db

    if db.ping():
        logger.info("Conexión con MongoDB establecida en %s", settings.safe_mongo_host)
        try:
            creados = ensure_indexes(db.db)
            # Los índices del proyecto M5 se crean en su propia base (local por omisión).
            if db.m5_db is not db.db:
                from app.comercial.ingesta import COLECCIONES_PROYECTO

                creados += ensure_indexes(db.m5_db, solo=COLECCIONES_PROYECTO)
            logger.info("Índices asegurados: %s", ", ".join(creados) or "ninguno nuevo")
        except Exception:  # noqa: BLE001 - no se bloquea el arranque por los índices
            logger.exception("No se pudieron crear los índices al arrancar")
        # Precalienta la caché de las vistas frecuentes con los filtros por omisión:
        # el primer visitante no paga el coste de las agregaciones completas.
        _precalentar(settings, db)
    else:
        logger.warning(
            "MongoDB no responde en %s; la API arranca en modo degradado "
            "(/api/v1/health/ready devolverá 503)",
            settings.safe_mongo_host,
        )

    try:
        yield
    finally:
        close_database()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        summary="Dashboards empresariales sobre MongoDB: rentabilidad comercial.",
        description=(
            "API de solo lectura para el dashboard de rentabilidad comercial.\n\n"
            "**Convenciones**: todos los importes viajan en centavos enteros con sufijo "
            "`_cent` y moneda única MXN; `margen_pct` está en puntos porcentuales "
            "(0–100) y es `null` cuando la venta neta es 0.\n\n"
            "**Datos**: el dataset servido es sintético y no representa información real "
            "de ninguna empresa."
        ),
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings

    try:
        from app.llm import limites

        limites.asegurar_indices(
            get_database().db.client[settings.llm_limites_db], settings
        )
    except Exception:  # noqa: BLE001 - un fallo aquí no debe impedir arrancar
        logger.warning("No se pudo preparar el índice de cuotas LLM", exc_info=False)

    # Las respuestas del Atlas vial pueden pesar más de un megabyte (decenas de
    # miles de puntos del mapa). Se comprimen en tránsito; el contenido no cambia.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(LimiteCuerpoMiddleware, maximo=settings.http_max_cuerpo_bytes)
    app.add_middleware(IPVisitanteMiddleware, settings=settings)

    if settings.origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
            max_age=600,
        )

    def respuesta_de_error(request: Request, contenido: dict, status: int) -> JSONResponse:
        """Error en JSON que conserva las cabeceras CORS cuando el origen es válido.

        Las excepciones no controladas las responde Starlette **por fuera** del
        middleware de CORS, así que la respuesta llegaba al navegador sin
        ``Access-Control-Allow-Origin``: el tablero informaba de un problema de
        CORS (o de «no se pudo contactar con la API») cuando el problema real era
        un 500 del servidor. Estas dos respuestas cierran ese hueco.
        """
        respuesta = JSONResponse(contenido, status_code=status)
        origen = request.headers.get("origin")
        if origen and origen in settings.origins:
            respuesta.headers["Access-Control-Allow-Origin"] = origen
            respuesta.headers["Vary"] = "Origin"
        return respuesta

    @app.exception_handler(PyMongoError)
    async def error_base_datos(request: Request, exc: PyMongoError) -> JSONResponse:
        """Un corte de MongoDB es un 503 reintentable, no un fallo del tablero."""
        logger.warning(
            "MongoDB no respondió en %s %s: %s",
            request.method,
            request.url.path,
            type(exc).__name__,
        )
        return respuesta_de_error(
            request,
            {
                "detail": (
                    "La base de datos no respondió a tiempo. Vuelve a intentarlo en "
                    "unos segundos: la consulta se puede repetir sin perder filtros."
                )
            },
            503,
        )

    @app.exception_handler(Exception)
    async def error_no_controlado(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Fallo no controlado en %s %s", request.method, request.url.path)
        return respuesta_de_error(
            request,
            {
                "detail": (
                    "Error interno de la API. El detalle quedó en el registro del "
                    "servidor; vuelve a intentarlo en unos segundos."
                )
            },
            500,
        )

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    # Telemetría anónima del portafolio (contadores y ranking de proyectos).
    app.include_router(visitas.router, prefix=settings.api_v1_prefix)

    @app.get("/", tags=["Salud"], summary="Índice de la API")
    def root() -> dict:
        return {
            "servicio": settings.app_name,
            "version": settings.app_version,
            "api": settings.api_v1_prefix,
            "documentacion": "/api/docs",
            "salud": f"{settings.api_v1_prefix}/health/ready",
        }

    return app


app = create_app()

