"""Data Incident Commander · flujo determinista y estado efímero.

El servicio modela una respuesta a incidentes de calidad de datos. Los tres
escenarios inyectan una falla en una **copia en memoria** del snapshot WDI; el
snapshot fuente permanece intacto. La aprobación solo simula una reparación sobre
una copia temporal y genera un recibo local.

La consulta a un proveedor compatible con OpenAI es **opcional y consultiva**:
solo se ofrece si `LLM_BASE_URL`, `LLM_API_KEY` y `LLM_MODEL` están configurados,
devuelve una recomendación validada (`quarantine`, `backfill` o `fullreload`) y
nunca aprueba, ejecuta ni escribe: la decisión humana y la simulación en memoria
siguen siendo las únicas vías de cambio.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import Settings

GDP = "NY.GDP.MKTP.KD.ZG"
EXPECTED_YEARS = set(range(2019, 2026))
KNOWN_CODES = {GDP, "FP.CPI.TOTL.ZG", "SL.UEM.TOTL.ZS"}
ALLOWED_PLANS = ("quarantine", "backfill", "fullreload")
BLOCKED_PLANS = ("forward",)

#: Plazo máximo de una consulta al proveedor compatible con OpenAI.
LLM_TIMEOUT_S = 30.0
#: Límites de la respuesta aceptada: texto acotado y listas cortas.
LLM_MAX_JUSTIFICACION = 1200
LLM_MAX_ITEM = 280
LLM_MAX_ITEMS = 6

SCENARIOS: dict[str, dict[str, Any]] = {
    "missing": {
        "name": "Observación 2025 ausente",
        "rule": "period_coverage = 100%",
        "fault": "Falta PIB 2025 en la copia de trabajo.",
        "expected": "7 años",
        "actual": "6 años",
        "severity": "SEV 2",
        "affected": "2 reportes + 1 servicio",
        "facts": "Una observación anual falta respecto al contrato de siete periodos.",
    },
    "duplicate": {
        "name": "Clave anual duplicada",
        "rule": "unique(country, code, year)",
        "fault": "Se inyectaron tres filas repetidas en staging.",
        "expected": "21 llaves únicas",
        "actual": "24 filas · 3 duplicadas",
        "severity": "SEV 3",
        "affected": "1 mart + 1 tablero",
        "facts": "Tres filas duplicadas inyectadas en la copia local para probar idempotencia.",
    },
    "schema": {
        "name": "Contrato de esquema alterado",
        "rule": "schema_contract = compatible",
        "fault": "Se alteró el tipo de una clave en la copia.",
        "expected": "6 campos tipados",
        "actual": "5 válidos · 1 drift",
        "severity": "SEV 2",
        "affected": "2 jobs + 2 tableros",
        "facts": "Cambio de tipo simulado; no ocurrió en el origen oficial.",
    },
}

PLAN_META: dict[str, dict[str, Any]] = {
    "quarantine": {
        "name": "Cuarentena + recarga de fuente",
        "label": "RECOMENDADO",
        "copy": "Aislamiento alto · traza completa · cero escritura externa",
        "scores": [96, 95, 77, 98, 99],
    },
    "backfill": {
        "name": "Backfill desde réplica",
        "label": "ALTERNATIVA",
        "copy": "Recuperación rápida · depende de réplica fresca",
        "scores": [76, 74, 92, 82, 77],
    },
    "fullreload": {
        "name": "Recarga integral",
        "label": "ALTERNATIVA",
        "copy": "Cobertura amplia · mayor radio y costo de cómputo",
        "scores": [90, 81, 48, 42, 87],
    },
    "forward": {
        "name": "Imputar valor anterior",
        "label": "NO APROBABLE",
        "copy": "Rápido, pero inventa continuidad y rompe la semántica",
        "scores": [51, 29, 99, 56, 36],
        "blocked_reason": "La política bloquea la imputación de valores.",
    },
}


class SnapshotError(RuntimeError):
    """El snapshot no existe o no cumple el contrato de 21 observaciones."""


class ScenarioError(ValueError):
    """El escenario no está en el catálogo."""


class RunNotFound(LookupError):
    """El run local no existe o se perdió al reiniciar el proceso."""


class RunConflict(RuntimeError):
    """El run ya no espera una decisión humana."""


class PlanBlocked(ValueError):
    """La política bloquea el plan o este no está permitido."""


class LlmNoConfigurado(RuntimeError):
    """Falta la configuración del proveedor compatible con OpenAI."""


class LlmUpstreamError(RuntimeError):
    """El proveedor no respondió, tardó demasiado o devolvió un sobre ilegible."""


class LlmRespuestaInvalida(ValueError):
    """La respuesta no cumple el esquema o propone un plan bloqueado."""


def _post_chat(
    settings: Settings,
    payload: dict[str, Any],
    *,
    timeout: float = LLM_TIMEOUT_S,
) -> dict[str, Any]:
    """POST a `/chat/completions` del proveedor configurado y devuelve el sobre.

    La credencial solo viaja en la cabecera `Authorization`; nunca se registra,
    se devuelve ni se incluye en el cuerpo de la respuesta de la API.
    """
    endpoint = f"{str(settings.llm_base_url).rstrip('/')}/chat/completions"
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as respuesta:
            sobre = json.loads(respuesta.read())
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise LlmUpstreamError(
            "La consulta al proveedor no respondió o devolvió datos ilegibles."
        ) from exc
    if not isinstance(sobre, dict):
        raise LlmUpstreamError("La respuesta del proveedor no es un objeto JSON.")
    return sobre


def _extraer_contenido(sobre: dict[str, Any]) -> str:
    try:
        contenido = sobre["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmUpstreamError(
            "La respuesta del proveedor no trae un mensaje utilizable."
        ) from exc
    if not isinstance(contenido, str) or not contenido.strip():
        raise LlmUpstreamError("La respuesta del proveedor llegó vacía.")
    return contenido


def _texto_acotado(valor: Any, *, campo: str, maximo: int) -> str:
    if not isinstance(valor, str):
        raise LlmRespuestaInvalida(f"El campo «{campo}» debe ser texto.")
    limpio = " ".join(valor.split())
    if not limpio:
        raise LlmRespuestaInvalida(f"El campo «{campo}» está vacío.")
    if len(limpio) > maximo:
        raise LlmRespuestaInvalida(
            f"El campo «{campo}» supera {maximo} caracteres."
        )
    return limpio


def _lista_acotada(valor: Any, *, campo: str) -> list[str]:
    if not isinstance(valor, list) or not valor:
        raise LlmRespuestaInvalida(f"El campo «{campo}» debe ser una lista no vacía.")
    if len(valor) > LLM_MAX_ITEMS:
        raise LlmRespuestaInvalida(
            f"El campo «{campo}» admite como máximo {LLM_MAX_ITEMS} elementos."
        )
    return [
        _texto_acotado(item, campo=f"{campo}[{indice}]", maximo=LLM_MAX_ITEM)
        for indice, item in enumerate(valor)
    ]


def validar_recomendacion(bruto: str) -> dict[str, Any]:
    """Valida el JSON del modelo y rechaza cualquier plan fuera del catálogo.

    `forward` queda explícitamente bloqueado: si el modelo lo propone, la
    respuesta se considera inválida y no se muestra como recomendación.
    """
    try:
        datos = json.loads(bruto)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LlmRespuestaInvalida("El modelo no devolvió un JSON válido.") from exc
    if not isinstance(datos, dict):
        raise LlmRespuestaInvalida("El modelo no devolvió un objeto JSON.")

    plan = str(datos.get("recommendation") or "").strip().lower()
    if plan in BLOCKED_PLANS:
        raise LlmRespuestaInvalida(
            "El modelo propuso un plan que la política bloquea; no se muestra la recomendación."
        )
    if plan not in ALLOWED_PLANS:
        raise LlmRespuestaInvalida(
            "La recomendación no es uno de los planes permitidos por la política."
        )

    return {
        "plan": plan,
        "plan_name": PLAN_META[plan]["name"],
        "justification": _texto_acotado(
            datos.get("justification"),
            campo="justification",
            maximo=LLM_MAX_JUSTIFICACION,
        ),
        "evidence": _lista_acotada(datos.get("evidence"), campo="evidence"),
        "risks": _lista_acotada(datos.get("risks"), campo="risks"),
    }


def canonical_digest(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        rows, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _leer_snapshot(ruta: Path) -> dict[str, Any]:
    if not ruta.exists():
        raise SnapshotError(f"No existe el snapshot WDI: {ruta}")
    try:
        data = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError("El snapshot WDI no es un JSON válido.") from exc
    rows = data.get("observations") or []
    if len(rows) != 21 or not data.get("sources"):
        raise SnapshotError("El snapshot debe conservar 21 observaciones y sus fuentes.")
    return data


class IncidentCommander:
    """Mantiene runs efímeros en memoria y nunca escribe fuera de ellos."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.data_path = Path(settings.incident_data_path)
        self.snapshot = _leer_snapshot(self.data_path)
        self.clean = copy.deepcopy(self.snapshot["observations"])
        self.source_digest = canonical_digest(self.clean)
        self.source_file_sha256 = hashlib.sha256(self.data_path.read_bytes()).hexdigest()
        self.runs: dict[str, dict[str, Any]] = {}

    def _source_file_intact(self) -> bool:
        try:
            return (
                hashlib.sha256(self.data_path.read_bytes()).hexdigest()
                == self.source_file_sha256
            )
        except OSError:
            return False

    # --------------------------------------------------------------- catálogo
    @staticmethod
    def _fault_copy(scenario_id: str, clean: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = copy.deepcopy(clean)
        if scenario_id == "missing":
            return [row for row in rows if not (row["indicator_code"] == GDP and row["year"] == 2025)]
        if scenario_id == "duplicate":
            duplicate = next(
                row for row in rows if row["indicator_code"] == GDP and row["year"] == 2023
            )
            return rows + [copy.deepcopy(duplicate) for _ in range(3)]
        if scenario_id == "schema":
            row = next(
                row for row in rows if row["indicator_code"] == GDP and row["year"] == 2024
            )
            row["indicator_code"] = 12345
            return rows
        raise ScenarioError("Escenario no reconocido.")

    @staticmethod
    def _keys(rows: list[dict[str, Any]]) -> list[tuple[Any, str]]:
        return [(row.get("year"), str(row.get("indicator_code"))) for row in rows]

    def _evaluate(
        self,
        rows: list[dict[str, Any]],
        *,
        approved: bool = False,
        plan_id: str | None = None,
    ) -> list[dict[str, Any]]:
        codes = {str(row.get("indicator_code")) for row in rows}
        years = {row["year"] for row in rows if str(row.get("indicator_code")) == GDP}
        duplicate_count = len(self._keys(rows)) - len(set(self._keys(rows)))
        allowed = plan_id in ALLOWED_PLANS if plan_id else True
        return [
            {"id": "period_coverage", "passed": EXPECTED_YEARS.issubset(years), "scope": "2019–2025"},
            {"id": "key_uniqueness", "passed": duplicate_count == 0, "duplicate_keys": duplicate_count},
            {"id": "catalog_codes", "passed": all(isinstance(row.get("indicator_code"), str) for row in rows)},
            {"id": "known_indicators", "passed": codes.issubset(KNOWN_CODES)},
            {"id": "source_snapshot_immutable", "passed": canonical_digest(self.clean) == self.source_digest and self._source_file_intact()},
            {"id": "write_scope", "passed": True, "external_writes": 0},
            {"id": "sql_read_only", "passed": True},
            {"id": "plan_allowlist", "passed": allowed},
            {"id": "evidence_linked", "passed": True},
            {"id": "lineage_bounded", "passed": True},
            {"id": "rollback_available", "passed": True},
            {"id": "human_approval_recorded", "passed": approved},
        ]

    def _matrix(self) -> list[dict[str, Any]]:
        checks = [
            "Reconciliación",
            "Idempotencia",
            "Alcance acotado",
            "Rollback",
            "Linaje preservado",
            "Revisión humana",
        ]
        grid = [
            ["P", "P", "P", "F"],
            ["P", "P", "P", "P"],
            ["P", "P", "W", "P"],
            ["P", "P", "P", "W"],
            ["P", "P", "W", "F"],
            ["P", "P", "P", "P"],
        ]
        plans = ["quarantine", "backfill", "fullreload", "forward"]
        return [
            {
                "criterion": criterion,
                "results": {
                    plan: ("pasa" if value == "P" else "revisar" if value == "W" else "bloquea")
                    for plan, value in zip(plans, row)
                },
            }
            for criterion, row in zip(checks, grid)
        ]

    @staticmethod
    def _plan_summary(plan_id: str) -> dict[str, Any]:
        meta = PLAN_META[plan_id]
        return {
            "id": plan_id,
            "name": meta["name"],
            "label": meta["label"],
            "copy": meta["copy"],
            "scores": meta["scores"],
            "scores_are_hypothetical": True,
            "blocked": plan_id in BLOCKED_PLANS,
        }

    # ------------------------------------------------------------------- API
    def salud(self) -> dict[str, Any]:
        return {
            "ok": True,
            "demo": "Data Incident Commander",
            "mode": "flujo-determinista-con-consulta-opcional",
            "llm": self.llm_configurado,
            "llm_model": (str(self.settings.llm_model).strip() or None) if self.llm_configurado else None,
            "llm_mode": "consulta-json-validada" if self.llm_configurado else None,
            "llm_executes": False,
            "atlas": False,
            "snapshot_date": self.snapshot.get("snapshot_date"),
            "dataset_id": self.snapshot.get("dataset_id"),
            "observations": len(self.clean),
            "source_digest": self.source_digest,
            "source_file_intact": self._source_file_intact(),
            "external_writes": 0,
            "active_runs": len(self.runs),
            "scenarios": list(SCENARIOS),
            "plans": list(PLAN_META),
            "blocked_plans": list(BLOCKED_PLANS),
        }

    def snapshot_summary(self) -> dict[str, Any]:
        return {
            "dataset_id": self.snapshot.get("dataset_id"),
            "snapshot_date": self.snapshot.get("snapshot_date"),
            "observation_count": len(self.clean),
            "source_digest": self.source_digest,
            "sources": self.snapshot.get("sources"),
            "period": [self.snapshot.get("period_start"), self.snapshot.get("period_end")],
        }

    def crear_run(self, scenario_id: str) -> dict[str, Any]:
        if scenario_id not in SCENARIOS:
            raise ScenarioError("Escenario no reconocido.")
        working = self._fault_copy(scenario_id, self.clean)
        run_id = "DI-" + uuid.uuid4().hex[:10].upper()
        baseline = self._evaluate(working)
        candidate = self._repair_copy(scenario_id, "quarantine")
        candidate_checks = self._evaluate(candidate, plan_id="quarantine")
        run = {
            "run_id": run_id,
            "scenario_id": scenario_id,
            "status": "awaiting_human",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_snapshot": self.snapshot.get("dataset_id"),
            "source_digest": self.source_digest,
            "working_copy": working,
            "clean_snapshot": copy.deepcopy(self.clean),
            "external_writes": 0,
        }
        if len(self.runs) >= 100:
            self.runs.pop(next(iter(self.runs)))
        self.runs[run_id] = run
        return {
            "run_id": run_id,
            "status": run["status"],
            "scenario": SCENARIOS[scenario_id],
            "observations_in_copy": len(working),
            "observations_in_source": len(self.clean),
            "source_digest": self.source_digest,
            "baseline_checks": baseline,
            "candidate_checks": candidate_checks,
            "recommended_plan": "quarantine",
            "allowed_plans": list(ALLOWED_PLANS),
            "blocked_plans": list(BLOCKED_PLANS),
            "plans": [self._plan_summary(plan) for plan in PLAN_META],
            "validation_matrix": self._matrix(),
            "write_authority": "none",
            "external_writes": 0,
            "tools": ["read_only_profiler", "lineage_fixture", "contract_validator"],
            "deterministic": True,
        }

    def _repair_copy(self, scenario_id: str, plan_id: str) -> list[dict[str, Any]]:
        if plan_id in BLOCKED_PLANS:
            raise PlanBlocked("La política bloquea la imputación de valores.")
        if plan_id not in ALLOWED_PLANS:
            raise PlanBlocked("La política bloqueó el plan seleccionado.")
        # La fuente limpia es la réplica de recuperación de esta simulación.
        # No se escribe nada: se devuelve una copia nueva para el sandbox.
        return copy.deepcopy(self.clean)

    def _run(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        if run is None:
            raise RunNotFound("Run local inexistente o reiniciado.")
        return run

    def aprobar(
        self,
        run_id: str,
        *,
        approved: bool,
        plan_id: str,
    ) -> dict[str, Any]:
        run = self._run(run_id)
        if run["status"] != "awaiting_human":
            raise RunConflict("El run ya no espera aprobación.")
        if not approved:
            # El rechazo siempre termina sin efecto, incluso si el plan
            # seleccionado era la imputación bloqueada.
            run["status"] = "rejected_no_effect"
            run["approval"] = {
                "human": True,
                "approved": False,
                "plan_id": plan_id,
                "decided_at": datetime.now(timezone.utc).isoformat(),
            }
            run["receipt"] = None
            return {
                "run_id": run_id,
                "status": run["status"],
                "receipt": None,
                "source_unchanged": canonical_digest(run["clean_snapshot"]) == self.source_digest and self._source_file_intact(),
                "external_writes": 0,
                "writes": [],
            }
        if plan_id in BLOCKED_PLANS:
            raise PlanBlocked(
                "La política bloquea la imputación de valores; no se ejecutó nada."
            )
        if plan_id not in ALLOWED_PLANS:
            raise PlanBlocked("La política bloqueó la remediación; no se ejecutó.")

        before = self._evaluate(run["working_copy"])
        candidate = self._repair_copy(run["scenario_id"], plan_id)
        after = self._evaluate(candidate, approved=True, plan_id=plan_id)
        if not all(check["passed"] for check in after):
            raise RunConflict("El candidato no pasó todas las validaciones.")
        receipt = "RCPT-" + uuid.uuid4().hex[:8].upper()
        run["status"] = "sandbox_simulation_completed"
        run["approval"] = {
            "human": True,
            "approved": True,
            "plan_id": plan_id,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
        run["receipt"] = receipt
        run["candidate"] = candidate
        run["external_writes"] = 0
        return {
            "run_id": run_id,
            "status": run["status"],
            "receipt": receipt,
            "plan_id": plan_id,
            "tests_before": before,
            "tests_after": after,
            "rows_before": len(run["working_copy"]),
            "rows_after": len(candidate),
            "source_unchanged": canonical_digest(run["clean_snapshot"]) == self.source_digest and self._source_file_intact(),
            "external_writes": 0,
            "writes": [],
            "simulation_only": True,
        }

    # ------------------------------------------------- consulta al proveedor LLM
    @property
    def llm_configurado(self) -> bool:
        """Las tres variables del proveedor deben estar presentes."""
        return bool(
            (self.settings.llm_base_url or "").strip()
            and (self.settings.llm_api_key or "").strip()
            and (self.settings.llm_model or "").strip()
        )

    def _contexto_consulta(self, run: dict[str, Any]) -> dict[str, Any]:
        """Resumen acotado del run: sin nombres, sin credenciales, sin datos crudos."""
        scenario = SCENARIOS[run["scenario_id"]]
        return {
            "run_id": run["run_id"],
            "scenario": {
                "id": run["scenario_id"],
                "name": scenario["name"],
                "rule": scenario["rule"],
                "fault": scenario["fault"],
                "expected": scenario["expected"],
                "actual": scenario["actual"],
                "severity": scenario["severity"],
                "affected": scenario["affected"],
            },
            "baseline_checks": [
                {"id": check["id"], "passed": check["passed"]}
                for check in self._evaluate(run["working_copy"])
            ],
            "plans_allowed": list(ALLOWED_PLANS),
            "plans_blocked": list(BLOCKED_PLANS),
            "write_authority": "none",
            "human_approval": "pending" if run["status"] == "awaiting_human" else "recorded",
            "external_writes": 0,
        }

    @staticmethod
    def _uso_tokens(sobre: dict[str, Any]) -> dict[str, int] | None:
        uso = sobre.get("usage")
        if not isinstance(uso, dict):
            return None
        limpio = {
            clave: valor
            for clave, valor in (
                ("prompt_tokens", uso.get("prompt_tokens")),
                ("completion_tokens", uso.get("completion_tokens")),
                ("total_tokens", uso.get("total_tokens")),
            )
            if isinstance(valor, int) and not isinstance(valor, bool)
        }
        return limpio or None

    def analizar(self, run_id: str) -> dict[str, Any]:
        """Pide una recomendación consultiva y validada al proveedor configurado.

        No aprueba, no ejecuta y no cambia el estado del run: solo añade una
        consulta a su traza. La aprobación humana y la simulación en memoria
        siguen siendo las únicas vías de cambio.
        """
        run = self._run(run_id)
        if not self.llm_configurado:
            raise LlmNoConfigurado(
                "La consulta a OpenAI no está configurada en este despliegue."
            )

        contexto = self._contexto_consulta(run)
        payload = {
            "model": str(self.settings.llm_model),
            "temperature": 0,
            "max_tokens": 700,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Eres un consejero de fiabilidad de datos. Analizas un incidente "
                        "ya diagnosticado y propones una remediación. Devuelve SOLO JSON "
                        'con el esquema {"recommendation":"quarantine|backfill|fullreload",'
                        '"justification":"...","evidence":["..."],"risks":["..."]}. '
                        "Solo puedes recomendar quarantine, backfill o fullreload: "
                        "«forward» (imputar valores) está bloqueado por la política y no "
                        "debes proponerlo ni describirlo como remediación válida. "
                        "La justificación debe ser una o dos frases; incluye entre una y "
                        "cuatro evidencias y entre una y cuatro riesgos, cada uno de una "
                        "frase corta. No apruebas, no ejecutas y no escribes datos: tu "
                        "respuesta es una opinión para revisión humana. Responde en español."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(contexto, ensure_ascii=False),
                },
            ],
        }

        sobre = _post_chat(self.settings, payload)
        contenido = _extraer_contenido(sobre)
        plan = validar_recomendacion(contenido)

        generado = datetime.now(timezone.utc).isoformat()
        consulta = {
            "consultation_id": "LLM-" + uuid.uuid4().hex[:8].upper(),
            "generated_at": generado,
            "model": str(self.settings.llm_model),
            **plan,
        }
        run["analysis"] = consulta
        run["analysis_count"] = int(run.get("analysis_count", 0)) + 1

        return {
            "run_id": run_id,
            "status": "consulta_completada",
            "consultation_id": consulta["consultation_id"],
            "generated_at": generado,
            "provider": "openai-compatible",
            "model": consulta["model"],
            "recommendation": {
                "plan": plan["plan"],
                "name": plan["plan_name"],
                "allowed": True,
                "blocked": False,
                "policy": list(ALLOWED_PLANS),
                "forward_blocked": True,
            },
            "justification": plan["justification"],
            "evidence": plan["evidence"],
            "risks": plan["risks"],
            "advisory_only": True,
            "approved": False,
            "executed": False,
            "approval_required": True,
            "human_approval": "pending" if run["status"] == "awaiting_human" else "recorded",
            "run_status": run["status"],
            "allowed_plans": list(ALLOWED_PLANS),
            "blocked_plans": list(BLOCKED_PLANS),
            "external_writes": 0,
            "source_unchanged": canonical_digest(run["clean_snapshot"]) == self.source_digest
            and self._source_file_intact(),
            "usage": self._uso_tokens(sobre),
            "charges_may_apply": True,
        }
