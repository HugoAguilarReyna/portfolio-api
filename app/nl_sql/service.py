"""Copiloto NL-to-SQL de indicadores WDI de México.

El texto libre **nunca** se convierte en SQL: un parser léxico reconoce un
subconjunto acotado de indicadores, años y operaciones. El SQL se construye
únicamente con plantillas permitidas, parámetros enlazados y una conexión SQLite
abierta en ``mode=ro``.

Atlas es opcional y solo recibe metadatos mínimos de ejecución en su colección
dedicada; no se guarda la pregunta libre. El modelo de lenguaje también es
opcional: si existe, solo puede devolver un plan JSON validado. Cualquier fallo
del modelo o del plan regresa al parser local.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.llm import cliente


class DatasetError(RuntimeError):
    """El snapshot local no existe o no cumple el contrato mínimo."""


class QuestionError(ValueError):
    """La pregunta no puede mapearse al catálogo acotado."""


#: Catálogo cerrado. El orden define el orden de las series en una comparación.
METRICS: dict[str, dict[str, Any]] = {
    "NY.GDP.MKTP.KD.ZG": {
        "title": "Crecimiento del PIB real",
        "terms": ("pib", "producto interno", "crecimiento", "economia"),
        "unit": "% anual",
    },
    "FP.CPI.TOTL.ZG": {
        "title": "Inflación al consumidor",
        "terms": ("inflacion", "precios", "ipc", "cpi"),
        "unit": "% anual",
    },
    "SL.UEM.TOTL.ZS": {
        "title": "Desempleo total · estimación OIT",
        "terms": ("desempleo", "desocupacion", "empleo", "sin trabajo", "oit"),
        "unit": "% fuerza laboral",
    },
}
SUPPORTED_YEARS = (2019, 2025)
UNSUPPORTED_COUNTRIES = (
    "guatemala",
    "honduras",
    "el salvador",
    "costa rica",
    "nicaragua",
    "panama",
    "estados unidos",
    "eeuu",
    "canada",
    "brasil",
    "argentina",
    "colombia",
)

_DB_LOCK = threading.Lock()


def normalizar(texto: str) -> str:
    """Minúsculas sin acentos, para el parser léxico."""
    return "".join(
        caracter
        for caracter in unicodedata.normalize("NFD", texto.lower())
        if unicodedata.category(caracter) != "Mn"
    )


def _leer_datos(ruta: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not ruta.exists():
        raise DatasetError(f"No existe el snapshot WDI: {ruta}")
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError("El snapshot WDI no es un JSON válido.") from exc

    fuentes = datos.get("sources") or []
    filas = datos.get("observations") or []
    if not fuentes or not filas:
        raise DatasetError("El snapshot WDI debe incluir sources y observations.")
    codigos = {fuente.get("indicator_code") for fuente in fuentes}
    if not set(METRICS).issubset(codigos):
        raise DatasetError("El snapshot WDI no contiene los tres indicadores esperados.")
    for fila in filas:
        if fila.get("indicator_code") not in METRICS or not isinstance(fila.get("year"), int):
            raise DatasetError("El snapshot WDI contiene una observación fuera del catálogo.")
    return datos, filas


class CopilotoNlSql:
    """Caso de uso único: snapshot, parser, SQL de plantilla, filas y salud."""

    def __init__(self, db: Any, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.data_path = Path(settings.nl_sql_data_path)
        self.db_path = Path(settings.nl_sql_db_path)
        self.data, self.rows = _leer_datos(self.data_path)
        self.by_code = {fuente["indicator_code"]: fuente for fuente in self.data["sources"]}
        self._preparar_sqlite()

    # ------------------------------------------------------------------ datos
    def _preparar_sqlite(self) -> None:
        """Crea/actualiza el índice SQLite local sin tocar el snapshot."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(self.data_path.read_bytes()).hexdigest()

        if self._hash_sqlite() == digest:
            return

        with _DB_LOCK:
            # Otra petición pudo preparar la base mientras se esperaba el lock.
            if self._hash_sqlite() == digest:
                return
            with sqlite3.connect(self.db_path) as con:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS observations ("
                    "country_code TEXT NOT NULL, country TEXT NOT NULL, "
                    "year INTEGER NOT NULL, indicator_code TEXT NOT NULL, "
                    "indicator TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL, "
                    "PRIMARY KEY(country_code, year, indicator_code))"
                )
                con.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                con.execute("DELETE FROM observations")
                con.executemany(
                    "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            "MEX",
                            self.data.get("geography", "México"),
                            fila["year"],
                            fila["indicator_code"],
                            fila["indicator"],
                            float(fila["value"]),
                            fila["unit"],
                        )
                        for fila in self.rows
                    ],
                )
                con.execute(
                    "INSERT INTO metadata(key, value) VALUES ('dataset_sha256', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (digest,),
                )
                con.commit()

    def _hash_sqlite(self) -> str | None:
        if not self.db_path.exists():
            return None
        try:
            with sqlite3.connect(f"file:{self.db_path.resolve()}?mode=ro", uri=True) as con:
                fila = con.execute(
                    "SELECT value FROM metadata WHERE key = 'dataset_sha256'"
                ).fetchone()
            return fila[0] if fila else None
        except sqlite3.Error:
            return None

    # --------------------------------------------------------------- parser
    @staticmethod
    def _rechazar_pais(pregunta: str) -> None:
        q = normalizar(pregunta)
        if any(pais in q for pais in UNSUPPORTED_COUNTRIES):
            raise QuestionError("El snapshot de esta demo solo cubre México.")

    def parse_local(self, pregunta: str) -> dict[str, Any]:
        """Parser léxico: catálogo cerrado, años explícitos y operación acotada."""
        q = normalizar(pregunta)
        self._validar_alcance(pregunta)
        indicadores = [
            codigo
            for codigo, meta in METRICS.items()
            if any(termino in q for termino in meta["terms"])
        ]
        if not indicadores:
            raise QuestionError(
                "No reconocí el indicador. Prueba con PIB, inflación o desempleo."
            )

        años = [int(año) for año in re.findall(r"\b(?:19|20)\d{2}\b", q)]
        if años and any(año < SUPPORTED_YEARS[0] or año > SUPPORTED_YEARS[1] for año in años):
            raise QuestionError(
                f"El periodo disponible es {SUPPORTED_YEARS[0]}–{SUPPORTED_YEARS[1]}."
            )
        if años:
            inicio, fin = min(años), max(años)
        else:
            inicio, fin = SUPPORTED_YEARS

        if any(
            palabra in q
            for palabra in ("mayor", "maximo", "maxima", "mas alto", "mas elevada", "pico")
        ):
            orden = "DESC"
        elif any(
            palabra in q
            for palabra in ("menor", "minimo", "minima", "mas bajo", "mas baja")
        ):
            orden = "ASC"
        else:
            orden = None

        if orden and len(indicadores) > 1:
            raise QuestionError(
                "Para ordenar por máximo o mínimo, especifica un solo indicador."
            )

        return {
            "country_code": "MEX",
            "country": "México",
            "indicator_codes": indicadores,
            "indicator_labels": [METRICS[codigo]["title"] for codigo in indicadores],
            "start_year": inicio,
            "end_year": fin,
            "kind": "ranking" if orden else ("comparison" if len(indicadores) > 1 else "trend"),
            "rank_order": orden,
        }

    @property
    def llm_configurado(self) -> bool:
        return bool(
            (self.settings.llm_base_url or "").strip()
            and (self.settings.llm_api_key or "").strip()
            and (self.settings.llm_model or "").strip()
        )

    def _plan_llm(self, pregunta: str) -> dict[str, Any] | None:
        """Pide al modelo un plan JSON y lo valida; nunca pide ni ejecuta SQL."""
        self._validar_alcance(pregunta)
        if not self.llm_configurado:
            return None
        permitidos = {codigo: meta["title"] for codigo, meta in METRICS.items()}
        sistema = (
            "Interpreta una consulta en español para el snapshot WDI de México. "
            "Devuelve SOLO JSON con el esquema "
            '{"country_code":"MEX","indicator_codes":[...],'
            '"start_year":2019,"end_year":2025,'
            '"operation":"trend|comparison|max|min"}. '
            "Usa solo códigos permitidos, años entre 2019 y 2025 y una operación "
            "compatible: max/min con un indicador, comparison con dos o tres. "
            "Si no puedes mapearla, devuelve {\"unsupported\":true}. "
            "Nunca generes SQL."
        )
        try:
            # Transporte, cuota por minuto y parseo JSON: cliente compartido.
            # La validación semántica del plan sigue siendo la de abajo.
            plan, _sobre = cliente.solicitar_json(
                self.settings,
                clave_limite="nl2sql",
                sistema=sistema,
                datos={
                    "question": pregunta,
                    "indicators_allowed": permitidos,
                    "years_allowed": list(SUPPORTED_YEARS),
                },
                validar=lambda datos: datos,
                max_tokens=min(int(self.settings.llm_max_tokens), 300),
            )
            if not isinstance(plan, dict) or plan.get("unsupported"):
                return None
            codigos = plan.get("indicator_codes")
            inicio = plan.get("start_year")
            fin = plan.get("end_year")
            operacion = plan.get("operation")
            if plan.get("country_code") != "MEX":
                return None
            if (
                not isinstance(codigos, list)
                or not codigos
                or len(codigos) > len(METRICS)
                or len(set(codigos)) != len(codigos)
                or any(codigo not in METRICS for codigo in codigos)
            ):
                return None
            if (
                isinstance(inicio, bool)
                or isinstance(fin, bool)
                or not isinstance(inicio, int)
                or not isinstance(fin, int)
                or inicio < SUPPORTED_YEARS[0]
                or fin > SUPPORTED_YEARS[1]
                or inicio > fin
            ):
                return None
            if operacion not in {"trend", "comparison", "max", "min"}:
                return None
            if operacion in {"max", "min"} and len(codigos) != 1:
                return None
            if operacion == "comparison" and len(codigos) < 2:
                return None
            if len(codigos) > 1 and operacion != "comparison":
                return None
            return {
                "country_code": "MEX",
                "country": "México",
                "indicator_codes": codigos,
                "indicator_labels": [METRICS[codigo]["title"] for codigo in codigos],
                "start_year": inicio,
                "end_year": fin,
                "kind": "ranking" if operacion in {"max", "min"} else operacion,
                "rank_order": "DESC" if operacion == "max" else "ASC" if operacion == "min" else None,
            }
        except Exception:
            # El proveedor no responde o devuelve un plan inválido: parser local.
            return None

    def resolver(self, pregunta: str) -> tuple[dict[str, Any], str]:
        self._validar_alcance(pregunta)
        plan = self._plan_llm(pregunta)
        if plan is not None:
            return plan, "llm-json-validado"
        return self.parse_local(pregunta), "reglas-locales"

    @staticmethod
    def _validar_alcance(pregunta: str) -> None:
        """Rechaza unidades, frecuencias e imperativos fuera del contrato anual.

        Es una guarda léxica explícita, no un clasificador semántico general. Se
        aplica también antes del modelo para no convertir una solicitud de otro
        indicador o de escritura en un SELECT aparentemente correcto.
        """
        CopilotoNlSql._rechazar_pais(pregunta)
        q = normalizar(pregunta)
        if re.search(r"\bper[\s-]*capita\b|\bpor habitante\b", q):
            raise QuestionError("El catálogo no incluye indicadores por habitante.")
        if re.search(r"\b(?:trimestral(?:es)?|trimestres?|mensual(?:es)?|semanal(?:es)?|diari[oa]s?)\b", q):
            raise QuestionError("El snapshot contiene observaciones anuales, no esa frecuencia.")
        if re.search(r"^\s*(?:(?:por favor|ahora|haz|realiza|ejecuta)\s+)*(?:borra(?:r)?|elimina(?:r)?|actualiza(?:r)?|modifica(?:r)?|sobrescribe|(?:un\s+)?backfill)\b", q):
            raise QuestionError("Solo se permiten consultas de lectura; no cambios ni backfill.")

    # ------------------------------------------------------------------- SQL
    def construir_sql(self, spec: dict[str, Any]) -> tuple[str, list[Any]]:
        codigos = spec["indicator_codes"]
        if not codigos or any(codigo not in METRICS for codigo in codigos):
            raise QuestionError("El plan contiene un indicador fuera del catálogo.")
        if spec["country_code"] != "MEX":
            raise QuestionError("El plan contiene un país fuera del snapshot.")
        marcadores = ", ".join("?" for _ in codigos)
        sql = (
            "SELECT year, country, indicator_code, indicator, value, unit\n"
            "FROM observations\n"
            "WHERE country_code = ?\n"
            f"  AND indicator_code IN ({marcadores})\n"
            "  AND year BETWEEN ? AND ?"
        )
        parametros: list[Any] = [
            spec["country_code"],
            *codigos,
            int(spec["start_year"]),
            int(spec["end_year"]),
        ]
        if spec["kind"] == "ranking":
            if spec.get("rank_order") not in {"ASC", "DESC"}:
                raise QuestionError("El plan contiene una operación de orden inválida.")
            sql += (
                f"\nORDER BY value {spec['rank_order']}, "
                f"year {'ASC' if spec['rank_order'] == 'DESC' else 'DESC'};"
            )
        elif spec["kind"] in {"trend", "comparison"}:
            sql += "\nORDER BY indicator_code, year;"
        else:
            raise QuestionError("El plan contiene una operación desconocida.")
        return sql, parametros

    def ejecutar_lectura(self, sql: str, parametros: list[Any]) -> list[dict[str, Any]]:
        if not sql.lstrip().upper().startswith("SELECT"):
            raise QuestionError("Solo se permiten consultas SELECT de plantilla.")
        with sqlite3.connect(f"file:{self.db_path.resolve()}?mode=ro", uri=True) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON")
            return [dict(fila) for fila in con.execute(sql, parametros).fetchall()]

    # --------------------------------------------------------------- resumen
    @staticmethod
    def resumen_local(spec: dict[str, Any], filas: list[dict[str, Any]]) -> str:
        if not filas:
            return "No hay observaciones para esos filtros en el corte local."
        if spec["kind"] == "ranking":
            fila = filas[0]
            etiqueta = "más alto" if spec["rank_order"] == "DESC" else "más bajo"
            return (
                f"El valor {etiqueta} del periodo fue {fila['value']:,.2f} "
                f"{fila['unit']} para {fila['indicator']} en {fila['year']}."
            )
        if spec["start_year"] == spec["end_year"]:
            partes = []
            for codigo in spec["indicator_codes"]:
                fila = next(
                    (fila for fila in filas if fila["indicator_code"] == codigo),
                    None,
                )
                if fila is not None:
                    partes.append(
                        f"En {fila['year']}, {fila['indicator']}: "
                        f"{fila['value']:,.2f} {fila['unit']}."
                    )
            return " ".join(partes)
        partes = []
        for codigo in spec["indicator_codes"]:
            serie = [fila for fila in filas if fila["indicator_code"] == codigo]
            if not serie:
                continue
            primero, ultimo = serie[0], serie[-1]
            delta = ultimo["value"] - primero["value"]
            direccion = (
                "aumentó" if delta > 0 else "disminuyó" if delta < 0 else "se mantuvo"
            )
            partes.append(
                f"{ultimo['indicator']}: {primero['value']:,.2f} {primero['unit']} "
                f"en {primero['year']} y {ultimo['value']:,.2f} {ultimo['unit']} "
                f"en {ultimo['year']}; {direccion} {abs(delta):,.2f} puntos "
                "porcentuales en el periodo."
            )
        return " ".join(partes)

    # ------------------------------------------------------------------ Atlas
    @staticmethod
    def _objetivo_mongo(uri: str) -> str:
        texto = (uri or "").lower()
        if "mongodb+srv://" in texto or "mongodb.net" in texto:
            return "atlas"
        return "local"

    def _ping(self) -> bool:
        if self.db is None:
            return False
        ping = getattr(self.db, "ping", None)
        if ping is None:
            return False
        try:
            return bool(ping() if callable(ping) else ping)
        except Exception:
            return False

    def _auditar(self, spec: dict[str, Any], filas: int, motor: str) -> str:
        """Registra metadatos mínimos, nunca la pregunta libre."""
        if not self.settings.nl_sql_audit_enabled or not self._ping():
            return "local"
        try:
            coleccion = self.db.client[self.settings.nl_sql_audit_db][
                self.settings.nl_sql_audit_collection
            ]
            coleccion.insert_one(
                {
                    "created_at": datetime.now(timezone.utc),
                    "indicators": spec["indicator_codes"],
                    "year_range": [spec["start_year"], spec["end_year"]],
                    "query_type": spec["kind"],
                    "result_rows": filas,
                    "engine": motor,
                    "snapshot_date": self.data.get("snapshot_date"),
                }
            )
            return "atlas"
        except Exception:
            return "local"

    # ------------------------------------------------------------------ API
    def salud(self) -> dict[str, Any]:
        return {
            "ok": True,
            "demo": "Copiloto NL-to-SQL · indicadores económicos de México",
            "dataset_id": self.data.get("dataset_id"),
            "snapshot_date": self.data.get("snapshot_date"),
            "period": [self.data.get("period_start"), self.data.get("period_end")],
            "observations": len(self.rows),
            "indicators": [
                {
                    "code": codigo,
                    "title": meta["title"],
                    "unit": meta["unit"],
                }
                for codigo, meta in METRICS.items()
            ],
            "parser": "lexico-plantillas",
            "sqlite": {"engine": "sqlite", "read_only": True},
            "atlas": {
                "target": self._objetivo_mongo(self.settings.mongodb_uri),
                "reachable": self._ping(),
                "database": self.settings.nl_sql_audit_db,
                "collection": self.settings.nl_sql_audit_collection,
                "enabled": self.settings.nl_sql_audit_enabled,
            },
            "llm": {"configured": self.llm_configurado, "mode": "plan-json-validado"},
        }

    def datos(self) -> dict[str, Any]:
        return self.data

    def consultar(self, pregunta: str) -> dict[str, Any]:
        spec, modo_intencion = self.resolver(pregunta)
        sql, parametros = self.construir_sql(spec)
        filas = self.ejecutar_lectura(sql, parametros)
        auditoria = self._auditar(spec, len(filas), modo_intencion)
        return {
            "spec": spec,
            "sql": sql,
            "parameters": parametros,
            "rows": filas,
            "row_count": len(filas),
            "summary": self.resumen_local(spec, filas),
            "summary_mode": "cálculo determinista",
            "intent_mode": modo_intencion,
            "sources": [self.by_code[codigo] for codigo in spec["indicator_codes"]],
            "snapshot_date": self.data.get("snapshot_date"),
            "safety": (
                "SQL parametrizado de plantilla permitida · "
                "conexión SQLite de solo lectura"
            ),
            "audit": auditoria,
        }
