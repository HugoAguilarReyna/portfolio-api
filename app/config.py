"""Configuración de la API.

Todos los valores se leen de variables de entorno (o de un archivo `.env`).
No hay credenciales escritas en el código: el único valor por defecto es una URI
local sin autenticación, pensada para desarrollo.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    """Ajustes de ejecución y límites de la API."""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Portafolio Hugo Aguilar · API de dashboards"
    app_version: str = "1.0.0"
    api_v1_prefix: str = "/api/v1"
    log_level: str = "info"

    # --- MongoDB -------------------------------------------------------------
    mongodb_uri: str = "mongodb://127.0.0.1:27017/hugo_portfolio_dev"
    mongodb_db: str = "hugo_portfolio_dev"
    mongo_timeout_ms: int = Field(default=3_000, ge=500, le=30_000)

    # --- CORS ----------------------------------------------------------------
    allowed_origins: str = "http://localhost:8000,http://127.0.0.1:8000"

    # --- Límites de consulta -------------------------------------------------
    default_page_size: int = Field(default=50, ge=1)
    max_page_size: int = Field(default=200, ge=1, le=1_000)
    max_export_rows: int = Field(default=50_000, ge=100, le=500_000)
    max_query_ms: int = Field(default=8_000, ge=500, le=60_000)
    max_filter_values: int = Field(default=50, ge=1, le=500)
    max_date_range_days: int = Field(default=2_000, ge=30, le=20_000)
    default_range_days: int = Field(default=365, ge=7)
    max_option_rows: int = Field(default=250, ge=10, le=2_000)

    # --- Atlas vial (SSC · Ciudad de México) ---------------------------------
    #: Carpeta de versiones descargadas y de la geometría de alcaldías.
    atlas_data_dir: str = str(BACKEND_DIR / "data" / "atlas")
    #: Catálogo oficial (API CKAN del portal de datos abiertos de la CDMX).
    atlas_catalogo_url: str = "https://datos.cdmx.gob.mx/api/3/action/package_show"
    atlas_dataset_id: str = (
        "hechos-de-transito-registrados-por-la-ssc-2024-serie-de-datos-ampliada-no-comparativa"
    )
    atlas_timeout_s: int = Field(default=90, ge=10, le=600)
    atlas_max_intentos: int = Field(default=3, ge=1, le=6)
    #: Minutos durante los que un proceso de sincronización reserva el cerrojo.
    atlas_bloqueo_min: int = Field(default=30, ge=5, le=240)
    #: Tope de puntos que el mapa puede recibir de una vez (el conjunto real
    #: publicado tiene 30 655 hechos; el tope solo evita respuestas sin límite).
    atlas_max_puntos: int = Field(default=80_000, ge=1_000, le=200_000)

    def atlas_dir(self) -> Path:
        """Carpeta de datos del Atlas vial (versiones descargadas y geometría)."""
        return Path(self.atlas_data_dir)

    # --- Marketing y clientes (GA4 · BigQuery) -------------------------------
    #: Proyecto de Google Cloud con el que se factura la consulta pública. Si se
    #: omite, la ingesta usa el proyecto activo de `gcloud`. Solo lo necesita la
    #: **extracción**: la API y el tablero leen de MongoDB (Atlas) y no de Google.
    marketing_gcp_project: str | None = None
    #: Rango a consultar; por omisión, el periodo que documenta la fuente.
    marketing_desde: str | None = None
    marketing_hasta: str | None = None
    #: Tope de eventos por día que se guardan para la tabla de detalle.
    marketing_eventos_por_dia: int = Field(default=1_500, ge=100, le=20_000)
    #: Carpeta de la copia original de la extracción (fuera de Git).
    marketing_data_dir: str = str(BACKEND_DIR / "data" / "marketing")
    #: Guardar el detalle completo de eventos (4.3 M documentos). Desactivado por
    #: omisión: el modelo base cabe en un clúster pequeño y el detalle necesario
    #: para recorridos vive en la colección de sesiones.
    marketing_guardar_eventos: bool = False
    #: Segundos de caché del backend por versión y filtros normalizados.
    marketing_cache_ttl_s: int = Field(default=300, ge=0, le=3_600)
    #: Muestreo determinista por usuario: 0 = conjunto completo, 5 = 5 % de usuarios.
    marketing_muestra_porcentaje: float = Field(default=0.0, ge=0, le=100)
    #: Semilla documentada del muestreo (se registra en el manifiesto).
    marketing_muestra_semilla: str = "ga4-muestra-2026"
    #: URI propia de este caso: permite que el tablero lea de Atlas aunque el resto
    #: de servicios de Compose use la instancia local. Vacía = MONGODB_URI.
    marketing_mongodb_uri: str | None = None
    #: Colección de base distinta para este caso (por omisión, las del portafolio).
    marketing_mongodb_db: str | None = None

    @model_validator(mode="after")
    def _resolver_alias_marketing(self) -> "Settings":
        """Permite que marketing herede la URI principal sin duplicar el secreto.

        ``MARKETING_MONGODB_URI=@MONGODB_URI`` usa exactamente la cadena de
        ``MONGODB_URI``; así el tablero puede leer de Atlas aunque el resto de
        servicios apunte a otra instancia por variable de entorno.
        """
        valor = (self.marketing_mongodb_uri or "").strip()
        if valor in {"@MONGODB_URI", "$MONGODB_URI"}:
            object.__setattr__(self, "marketing_mongodb_uri", self.mongodb_uri)
        return self

    # --- Briefing con evidencia · Banco de México (RAG) --------------------
    #: Base dedicada de la demo. No usa MONGODB_DB: el corpus RAG vive aparte.
    rag_banxico_mongodb_db: str = "portfolio_demos"
    #: Colección dedicada; la carga es idempotente por chunk_id.
    rag_banxico_collection: str = "rag_banxico_chunks"
    #: Corpus versionable de la demo (fuente única para frontend, API y carga).
    rag_banxico_corpus_path: str = str(
        BACKEND_DIR / "data" / "snapshots" / "banxico" / "corpus.json"
    )
    #: Proveedor opcional compatible con OpenAI. Sin las tres variables, la
    #: respuesta se construye solo con los fragmentos recuperados.
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    #: Límites comunes a todas las demos que consultan al modelo.
    llm_timeout_s: float = Field(default=30.0, ge=5.0, le=120.0)
    #: Tope de salida por consulta; el cliente nunca envía más que este valor.
    llm_max_tokens: int = Field(default=300, ge=64, le=2_000)
    #: Consultas por minuto y por proceso (ventana deslizante, segunda barrera).
    llm_consultas_por_minuto: int = Field(default=5, ge=1, le=120)
    #: Clave estable (hex) para el HMAC de los identificadores de cuota.
    #: Sin ella las rutas LLM fallan cerrado: no se guarda ninguna IP en claro.
    llm_cuota_hmac_key: str | None = None
    #: Intervalo mínimo entre consultas de un mismo visitante, en segundos.
    llm_intervalo_minimo_s: int = Field(default=600, ge=0, le=86_400)
    #: Bucle de agente con herramientas (Incident Commander).
    llm_agente_max_pasos: int = Field(default=6, ge=1, le=12)
    #: Presupuesto de tokens del run (entrada+salida sumadas por el bucle).
    #: PROVISIONAL hasta medir un run real: sale del peor caso medido con
    #: `python scripts/medir_prompt_agente.py` (contexto al tope, 6 pasos y
    #: resultados de herramienta al tope). Sustitúyelo por el valor medido con
    #: un run real leyendo `tokens_consumidos` de la respuesta.
    llm_agente_presupuesto_tokens: int = Field(default=23_000, ge=300, le=200_000)
    #: Cuotas compartidas entre workers (base y colección dedicadas de la app).
    llm_limites_db: str = "portfolio_guard"
    llm_limites_coleccion: str = "llm_limites"
    llm_max_solicitudes_por_ip_dia: int = Field(default=1, ge=1, le=10_000)
    llm_max_solicitudes_dia: int = Field(default=10, ge=1, le=100_000)
    #: Tope diario de tokens para toda la API (10 consultas × 300 = 3 000).
    #: Debe cubrir al menos un run del agente (LLM_AGENTE_PRESUPUESTO_TOKENS).
    llm_max_tokens_dia: int = Field(default=230_000, ge=1_000, le=50_000_000)
    #: Redes cuyo `X-Real-IP`/`X-Forwarded-For` se acepta (proxy del despliegue).
    llm_proxies_confiables: str = (
        "127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    )
    #: Tope de cuerpo HTTP aceptado; por encima se responde 413.
    http_max_cuerpo_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)

    # --- Copiloto NL-to-SQL · indicadores WDI de México --------------------
    #: Snapshot versionable: fuente única para la interfaz, la API y SQLite.
    nl_sql_data_path: str = str(
        BACKEND_DIR / "data" / "snapshots" / "copiloto" / "data.json"
    )
    #: Índice SQLite generado en tiempo de ejecución (backend/data está ignorado).
    nl_sql_db_path: str = str(
        BACKEND_DIR / "data" / "copiloto_nl_sql" / "wdi.sqlite3"
    )
    #: Auditoría opcional de metadatos; no guarda la pregunta libre.
    nl_sql_audit_enabled: bool = True
    nl_sql_audit_db: str = "portfolio_demos"
    nl_sql_audit_collection: str = "nl_sql_query_audit"

    # --- Data Incident Commander (demo determinista) ------------------------
    #: Snapshot WDI real de 21 observaciones; las fallas se inyectan en copias.
    incident_data_path: str = str(
        BACKEND_DIR / "data" / "snapshots" / "incident-commander" / "data.json"
    )

    # --- Atlas comercial · Demanda y ventas (Walmart M5) ---------------------
    #: Carpeta con los CSV de M5 (no se versionan: datos de la competición).
    comercial_data_dir: str = str(BACKEND_DIR / "data" / "m5")
    #: Segundos de caché del backend por versión y filtros normalizados.
    comercial_cache_ttl_s: int = Field(default=120, ge=0, le=3_600)
    #: Tope de productos por página en el catálogo.
    comercial_max_productos: int = Field(default=200, ge=10, le=2_000)
    #: Destino exclusivo del proyecto M5. **No** hereda MONGODB_URI: si falta, se usa
    #: el MongoDB local con base propia y `app.comercial.conexion` valida el destino
    #: antes de abrir la conexión (rechaza remotos y mongodb+srv).
    comercial_mongodb_uri: str = "mongodb://127.0.0.1:27017/atlas_comercial_local"
    #: Base del proyecto; debe coincidir con la de la URI (se comprueba).
    comercial_mongodb_db: str | None = "atlas_comercial_local"
    #: Ventanas de backtesting del baseline.
    comercial_ventanas: int = Field(default=6, ge=1, le=24)

    def comercial_dir(self) -> Path:
        """Carpeta con los CSV originales de M5."""
        return Path(self.comercial_data_dir)

    def marketing_dir(self) -> Path:
        """Carpeta de la copia original de la extracción."""
        return Path(self.marketing_data_dir)

    @field_validator("marketing_mongodb_uri")
    @classmethod
    def _marketing_uri_valida(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if value.strip() in {"@MONGODB_URI", "$MONGODB_URI"}:
            # Alias resuelto más tarde, en el validador de modelo.
            return value.strip()
        if not value.startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError(
                "MARKETING_MONGODB_URI debe empezar por mongodb:// o mongodb+srv://"
            )
        return value

    @field_validator("mongodb_uri")
    @classmethod
    def _uri_no_vacia(cls, value: str) -> str:
        if not value or not value.startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError("MONGODB_URI debe empezar por mongodb:// o mongodb+srv://")
        return value

    @property
    def origins(self) -> list[str]:
        """Orígenes permitidos por CORS, ya normalizados."""
        return [origen.strip() for origen in self.allowed_origins.split(",") if origen.strip()]

    @property
    def safe_mongo_host(self) -> str:
        """Host de MongoDB sin credenciales, para diagnósticos."""
        uri = self.mongodb_uri
        if "@" in uri:
            resto = uri.split("@", 1)[1]
        else:
            resto = uri.split("://", 1)[-1]
        return resto.split("/", 1)[0]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Configuración cacheada: se crea una sola vez por proceso."""
    return Settings()

