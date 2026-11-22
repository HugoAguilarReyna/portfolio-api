"""Conexión a MongoDB y utilidades de acceso.

El frontend nunca habla con MongoDB: solo esta API. El cliente se crea una vez y
se comparte entre peticiones; los controladores síncronos de FastAPI se ejecutan
en un hilo del pool, así que PyMongo (síncrono) es seguro aquí.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import AutoReconnect, NetworkTimeout, PyMongoError

from app.comercial.conexion import resolver_destino
from app.config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Fallos de red que no significan «el dato no existe»: la consulta se puede
#: repetir tal cual. Un clúster de Atlas cierra conexiones inactivas o cambia de
#: nodo (elección de primario) y el reintento evita un 500 en esa petición.
ERRORES_TRANSITORIOS = (AutoReconnect, NetworkTimeout)


def reintentar_lectura(
    operacion: Callable[[], T],
    *,
    intentos: int = 3,
    espera_s: float = 0.4,
) -> T:
    """Ejecuta una lectura idempotente tolerando cortes transitorios de MongoDB.

    Solo se reintenta lo que es seguro repetir: lectura pura, sin escrituras ni
    efectos. El último intento propaga la excepción, que la API traduce a 503.
    """
    for numero in range(1, intentos + 1):
        try:
            return operacion()
        except ERRORES_TRANSITORIOS as error:
            if numero >= intentos:
                raise
            logger.warning(
                "Corte transitorio de MongoDB (%s, intento %s/%s): se repite la lectura",
                type(error).__name__,
                numero,
                intentos,
            )
            time.sleep(espera_s * numero)
    raise AssertionError("inalcanzable")  # pragma: no cover - el bucle siempre retorna o lanza


SALES_COLLECTION = "sales"
DATASETS_COLLECTION = "datasets"

# --- CNBV (datos públicos reales) -------------------------------------------
#: Importaciones: URL, hash, fecha de descarga, periodo y revisión.
CNBV_BOLETINES = "cnbv_boletines"
#: Filas originales del boletín, tal como vienen en el archivo.
CNBV_FILAS = "cnbv_filas_originales"
#: Hechos normalizados (periodo × institución × concepto).
CNBV_HECHOS = "cnbv_hechos"
#: Indicadores (porcentajes) publicados por la CNBV.
CNBV_INDICADORES = "cnbv_indicadores"

# --- Atlas vial (SSC · Ciudad de México, datos públicos reales) --------------
#: Una versión por importación: metadatos del catálogo, hashes, validación.
ATLAS_IMPORTACIONES = "atlas_importaciones"
#: Un documento por hecho de tránsito (misma granularidad que el archivo).
ATLAS_HECHOS = "atlas_hechos"
#: Puntero a la versión publicada (se cambia de forma atómica al publicar).
ATLAS_ESTADO = "atlas_estado"
#: Cerrojo para que dos sincronizaciones no corran a la vez.
ATLAS_BLOQUEOS = "atlas_bloqueos"
# --- Marketing y clientes (GA4 · BigQuery) -----------------------------------
#: Versiones publicadas de la ingesta del conjunto GA4.
MARKETING_IMPORTACIONES = "marketing_importaciones"
#: Agregado diario por dimensiones (base de tarjetas, serie y adquisición).
MARKETING_RESUMEN = "marketing_resumen"
#: Pares usuario-día-dimensión con el usuario **hasheado** (solo para contar
#: usuarios distintos; el identificador original no se guarda).
MARKETING_USUARIOS = "marketing_usuarios"
#: Compras deduplicadas sobre todo el periodo (una por usuario + transacción).
#: Es la única fuente de compras, compradores e ingresos: así el total no depende
#: de sumar días y se puede conciliar con una consulta de control.
MARKETING_COMPRAS = "marketing_compras"
#: Sesiones reconstruidas con los marcadores de tiempo de cada paso del embudo.
MARKETING_SESIONES = "marketing_sesiones"
#: Modelo compacto: flujo de sesiones por canal, dispositivo y resultado.
MARKETING_FLUJO = "marketing_flujo"
#: Modelo compacto: tiempos entre etapas por transición y tramo.
MARKETING_TIEMPOS = "marketing_tiempos"
#: Modelo compacto: compras conjuntas por par de categorías.
MARKETING_AFINIDAD = "marketing_afinidad"
#: Líneas de artículo de las compras deduplicadas.
MARKETING_ARTICULOS = "marketing_articulos"
#: Sesiones que alcanzan cada paso del embudo.
MARKETING_EMBUDO = "marketing_embudo"
#: Retención y recompra por cohorte semanal.
MARKETING_COHORTES = "marketing_cohortes"
#: Eventos y compras por categoría de producto.
MARKETING_CATEGORIAS = "marketing_categorias"
#: Muestra de eventos para la tabla de detalle (sin identificadores de persona).
MARKETING_EVENTOS = "marketing_eventos"
#: Puntero a la versión publicada.
MARKETING_ESTADO = "marketing_estado"
#: Cerrojo de la ingesta.
MARKETING_BLOQUEOS = "marketing_bloqueos"

# --- Pulso comercial · Walmex ------------------------------------------------
#: Hechos comerciales agrupados: periodo × mercado × indicador × base.
WALMEX_HECHOS = "walmex_hechos"
#: Catálogo publicado (indicadores, mercados, periodos y combinaciones).
WALMEX_CATALOGO = "walmex_catalogo"
#: Documentos oficiales con URL, hash, fecha y páginas de referencia.
WALMEX_FUENTES = "walmex_fuentes"
#: Manifiesto de cada versión publicada.
WALMEX_IMPORTACIONES = "walmex_importaciones"
#: Puntero a la versión activa.
WALMEX_ESTADO = "walmex_estado"

# --- Atlas comercial · Demanda y ventas (Walmart M5) -------------------------
#: Manifiesto de cada importación: archivos, hashes, cobertura, validaciones.
M5_IMPORTACIONES = "m5_importaciones"
#: Puntero atómico a la versión publicada.
M5_ESTADO = "m5_estado"
#: Catálogo: tiendas, estados, categorías, departamentos y productos.
M5_CATALOGO = "m5_catalogo"
#: Un documento por día comercial (fecha real, semana, evento, snap).
M5_CALENDARIO = "m5_calendario"
#: Agregados diarios por dimensión: un documento por clave con el vector de días.
M5_AGREGADOS = "m5_agregados"
#: Serie producto × tienda en bloques acotados de días.
M5_BLOQUES = "m5_bloques"
#: Pronósticos versionados del baseline (backtesting y corte final).
M5_PRONOSTICOS = "m5_pronosticos"
#: Métricas de evaluación por ventana y por agregado.
M5_EVALUACION = "m5_evaluacion"
#: Estado de los derivados calculados en local (construcción y publicación).
M5_DERIVADOS = "m5_derivados"
#: Precios de venta por semana (``store_id × wm_yr_wk``).
M5_PRECIOS = "m5_precios"
#: Catálogo estable de artículos con precio por tienda (esquema compacto del paquete de demo).
M5_PRECIOS_CATALOGO = "m5_precios_catalogo"
#: Manifiesto de la importación de precios (hash, conteos, cobertura).
M5_PRECIOS_MANIFEST = "m5_precios_manifest"
#: Ejecuciones del modelo predictivo (metadatos, configuración y métricas).
M5_MODELOS = "m5_modelos"
#: Predicciones agregadas por dimensión y origen (vectores de 28 días).
M5_PREDICCIONES = "m5_predicciones"
#: Predicciones artículo × tienda de la ejecución publicada (solo el origen final).
M5_PREDICCIONES_SERIES = "m5_predicciones_series"
#: Evaluación del modelo por origen, horizonte y segmento.
M5_EVALUACION_MODELOS = "m5_evaluacion_modelos"
#: Derivado de planificación: una fila por artículo × tienda con el horizonte de 28 días
#: (observado previo, pronóstico, baseline y las cuatro semanas). Se genera con
#: ``scripts/derivar_planificacion.py`` desde el modelo publicado; nunca se escribe a mano.
M5_PLANIFICACION = "m5_planificacion"
#: Derivado de evaluación a 28 días por serie y corte (suma del horizonte, no de errores
#: diarios). Misma generación idempotente que la planificación.
M5_EVALUACION_SERIES = "m5_evaluacion_series"
#: Auditoría de separación temporal (evidencia por ejecución y corte).
M5_AUDITORIA_TEMPORAL = "m5_auditoria_temporal"
#: Revisiones de publicación de las predicciones (política de precisión y activación).
M5_PUBLICACION = "m5_publicacion"


class MongoDatabase:
    """Envoltorio mínimo de la base de datos de la aplicación."""

    def __init__(
        self,
        uri: str,
        db_name: str,
        timeout_ms: int = 3_000,
        *,
        marketing_uri: str | None = None,
        marketing_db: str | None = None,
        comercial_uri: str | None = None,
        comercial_db: str | None = None,
    ) -> None:
        self._client = MongoClient(
            uri,
            serverSelectionTimeoutMS=timeout_ms,
            connectTimeoutMS=timeout_ms,
            socketTimeoutMS=60_000,
            appname="hugo-portfolio-api",
            # Las fechas se guardan como UTC ingenuo (medianoche del día).
            tz_aware=False,
            # Compresión de red (útil sobre todo con un clúster remoto tipo Atlas).
            compressors="zlib",
        )
        self._db: Database = self._client[db_name]
        # El tablero de marketing puede leer de otro servidor (Atlas) sin que el resto
        # de servicios cambie: solo se crea un segundo cliente si hay URI propia.
        # El proyecto M5 vive en su propia base (por omisión, MongoDB local):
        # así los datos comerciales no se mezclan con los demás tableros.
        self._m5_client: MongoClient | None = None
        self._m5_db: Database = self._db
        # Destino del proyecto M5: se valida ANTES de crear el cliente (rechaza
        # remotos y mongodb+srv) y nunca reutiliza la base de los demás tableros.
        destino_m5, base_m5 = resolver_destino(comercial_uri, comercial_db)
        self._m5_client = MongoClient(
            destino_m5,
            serverSelectionTimeoutMS=timeout_ms,
            connectTimeoutMS=timeout_ms,
            socketTimeoutMS=60_000,
            appname="hugo-portfolio-m5",
            tz_aware=False,
            compressors="zlib",
        )
        self._m5_db = self._m5_client[base_m5]
        self._marketing_client: MongoClient | None = None
        if marketing_uri and marketing_uri != uri:
            self._marketing_client = MongoClient(
                marketing_uri,
                serverSelectionTimeoutMS=timeout_ms,
                connectTimeoutMS=timeout_ms,
                socketTimeoutMS=60_000,
                appname="hugo-portfolio-marketing",
                tz_aware=False,
                compressors="zlib",
            )
            self._marketing_db: Database = self._marketing_client[marketing_db or db_name]
        else:
            self._marketing_db = self._db

    @property
    def client(self) -> MongoClient:
        return self._client

    @property
    def db(self) -> Database:
        return self._db

    @property
    def marketing_db(self) -> Database:
        """Base que usa el tablero: Atlas si hay URI propia, si no la de la app."""
        return self._marketing_db

    @property
    def m5_db(self) -> Database:
        """Base exclusiva de M5: loopback local o servicio Docker mongo-m5."""
        return self._m5_db

    @property
    def usa_atlas_marketing(self) -> bool:
        return self._marketing_client is not None

    @property
    def sales(self) -> Collection:
        return self._db[SALES_COLLECTION]

    @property
    def datasets(self) -> Collection:
        return self._db[DATASETS_COLLECTION]

    # --- Atlas vial ----------------------------------------------------------
    @property
    def atlas_hechos(self) -> Collection:
        return self._db[ATLAS_HECHOS]

    @property
    def atlas_importaciones(self) -> Collection:
        return self._db[ATLAS_IMPORTACIONES]

    @property
    def atlas_estado(self) -> Collection:
        return self._db[ATLAS_ESTADO]

    @property
    def atlas_bloqueos(self) -> Collection:
        return self._db[ATLAS_BLOQUEOS]

    # --- Marketing y clientes (GA4) -----------------------------------------
    @property
    def marketing_resumen(self) -> Collection:
        return self._db[MARKETING_RESUMEN]

    # --- Atlas comercial · M5 ------------------------------------------------
    @property
    def m5_importaciones(self) -> Collection:
        return self._m5_db[M5_IMPORTACIONES]

    @property
    def m5_estado(self) -> Collection:
        return self._m5_db[M5_ESTADO]

    @property
    def m5_catalogo(self) -> Collection:
        return self._m5_db[M5_CATALOGO]

    @property
    def m5_calendario(self) -> Collection:
        return self._m5_db[M5_CALENDARIO]

    @property
    def m5_agregados(self) -> Collection:
        return self._m5_db[M5_AGREGADOS]

    @property
    def m5_bloques(self) -> Collection:
        return self._m5_db[M5_BLOQUES]

    @property
    def m5_pronosticos(self) -> Collection:
        return self._m5_db[M5_PRONOSTICOS]

    @property
    def m5_evaluacion(self) -> Collection:
        return self._m5_db[M5_EVALUACION]

    @property
    def m5_derivados(self) -> Collection:
        return self._m5_db[M5_DERIVADOS]

    @property
    def m5_precios(self) -> Collection:
        return self._m5_db[M5_PRECIOS]

    @property
    def m5_precios_manifest(self) -> Collection:
        return self._m5_db[M5_PRECIOS_MANIFEST]

    @property
    def m5_modelos(self) -> Collection:
        return self._m5_db[M5_MODELOS]

    @property
    def m5_predicciones(self) -> Collection:
        return self._m5_db[M5_PREDICCIONES]

    @property
    def m5_predicciones_series(self) -> Collection:
        return self._m5_db[M5_PREDICCIONES_SERIES]

    @property
    def m5_evaluacion_modelos(self) -> Collection:
        return self._m5_db[M5_EVALUACION_MODELOS]

    @property
    def marketing_usuarios(self) -> Collection:
        return self._db[MARKETING_USUARIOS]

    @property
    def marketing_compras(self) -> Collection:
        return self._db[MARKETING_COMPRAS]

    @property
    def marketing_sesiones(self) -> Collection:
        return self._db[MARKETING_SESIONES]

    @property
    def marketing_articulos(self) -> Collection:
        return self._db[MARKETING_ARTICULOS]

    @property
    def marketing_embudo(self) -> Collection:
        return self._db[MARKETING_EMBUDO]

    @property
    def marketing_cohortes(self) -> Collection:
        return self._db[MARKETING_COHORTES]

    @property
    def marketing_categorias(self) -> Collection:
        return self._db[MARKETING_CATEGORIAS]

    @property
    def marketing_eventos(self) -> Collection:
        return self._db[MARKETING_EVENTOS]

    @property
    def marketing_importaciones(self) -> Collection:
        return self._db[MARKETING_IMPORTACIONES]

    @property
    def marketing_estado(self) -> Collection:
        return self._db[MARKETING_ESTADO]

    @property
    def marketing_bloqueos(self) -> Collection:
        return self._db[MARKETING_BLOQUEOS]

    def ping(self) -> bool:
        """Comprueba la conexión sin propagar detalles internos."""
        try:
            self._client.admin.command("ping")
            return True
        except PyMongoError as exc:  # pragma: no cover - depende del entorno
            logger.warning("MongoDB no responde: %s", type(exc).__name__)
            return False

    def close(self) -> None:
        if self._m5_client is not None:
            self._m5_client.close()
            self._m5_client = None
        if self._marketing_client is not None:
            self._marketing_client.close()
            self._marketing_client = None
        self._client.close()


_database: MongoDatabase | None = None


def init_database(settings: Settings) -> MongoDatabase:
    """Crea (una vez) el cliente compartido."""
    global _database
    if _database is None:
        _database = MongoDatabase(
            settings.mongodb_uri,
            settings.mongodb_db,
            settings.mongo_timeout_ms,
            marketing_uri=settings.marketing_mongodb_uri,
            marketing_db=settings.marketing_mongodb_db,
            comercial_uri=settings.comercial_mongodb_uri,
            comercial_db=settings.comercial_mongodb_db,
        )
    return _database


def get_database() -> MongoDatabase:
    """Dependencia de FastAPI: devuelve la base de datos activa."""
    if _database is None:  # pragma: no cover - solo si falta el arranque
        raise RuntimeError("La base de datos no está inicializada")
    return _database


def close_database() -> None:
    global _database
    if _database is not None:
        _database.close()
        _database = None
