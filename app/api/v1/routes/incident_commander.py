"""API local de «Data Incident Commander».

Los runs viven solo en memoria del proceso. No hay Atlas, archivos de salida ni
escrituras externas: la aprobación simula una reparación sobre una copia temporal
y devuelve un recibo local.

La consulta a un proveedor compatible con OpenAI (`LLM_BASE_URL`, `LLM_MODEL`,
`LLM_API_KEY`) es **opcional y consultiva**: devuelve una recomendación validada
entre `quarantine`, `backfill` y `fullreload` —`forward` sigue bloqueado— y nunca
aprueba, ejecuta ni cambia el estado del run.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.config import Settings
from app.llm import cliente
from app.incident_commander.service import (
    IncidentCommander,
    LlmNoConfigurado,
    LlmRespuestaInvalida,
    LlmUpstreamError,
    PlanBlocked,
    RunConflict,
    RunNotFound,
    ScenarioError,
    SnapshotError,
)

router = APIRouter(
    prefix="/incident-commander",
    tags=["Data Incident Commander · AgentOps local"],
)


@lru_cache(maxsize=1)
def _servicio_incidentes() -> IncidentCommander:
    """Un único servicio por proceso: los runs son efímeros, pero compartidos.

    Si se creara una instancia por petición, el run desaparecería entre
    ``POST /runs`` y ``POST /runs/{id}/approve``. El caché se vacía al reiniciar
    el proceso, que es precisamente el límite declarado para esta demo. No se
    abre el archivo .env: el snapshot tiene ruta por defecto en el repositorio.
    """
    return IncidentCommander(Settings(_env_file=None))


def get_incident_service() -> IncidentCommander:
    try:
        return _servicio_incidentes()
    except SnapshotError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


IncidentDep = Annotated[IncidentCommander, Depends(get_incident_service)]


class RunRequest(BaseModel):
    scenario_id: Literal["missing", "duplicate", "schema"]


class ApprovalRequest(BaseModel):
    approved: bool
    plan_id: str = Field(min_length=3, max_length=32)


@router.get("/health", summary="Estado local y snapshot")
def health(servicio: IncidentDep) -> dict:
    return servicio.salud()


@router.get("/snapshot", summary="Metadatos del snapshot fuente")
def snapshot(servicio: IncidentDep) -> dict:
    return servicio.snapshot_summary()


@router.post("/runs", summary="Inicia una investigación determinista")
def create_run(servicio: IncidentDep, body: RunRequest) -> dict:
    try:
        return servicio.crear_run(body.scenario_id)
    except ScenarioError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.post("/runs/{run_id}/approve", summary="Rechaza o simula una aprobación")
def approve_run(run_id: str, body: ApprovalRequest, servicio: IncidentDep) -> dict:
    try:
        return servicio.aprobar(
            run_id,
            approved=body.approved,
            plan_id=body.plan_id,
        )
    except RunNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RunConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PlanBlocked as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.post(
    "/runs/{run_id}/analysis",
    summary="Consulta consultiva al proveedor compatible con OpenAI",
)
def analyse_run(run_id: str, servicio: IncidentDep) -> dict:
    """Devuelve una recomendación validada; nunca aprueba ni ejecuta."""
    try:
        return servicio.analizar(run_id)
    except RunNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except LlmNoConfigurado as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except cliente.CuotaExcedida as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.reintentar_en)},
        ) from exc
    except (cliente.LimitesNoConfigurados, cliente.LimitesNoDisponibles) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except cliente.LlmLimiteExcedido as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    except LlmUpstreamError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except LlmRespuestaInvalida as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc


@router.post(
    "/runs/{run_id}/agent",
    summary="Agente de solo lectura con herramientas (máximo 6 pasos)",
)
def run_agent(run_id: str, servicio: IncidentDep) -> dict:
    """Bucle con herramientas de lectura; no aprueba ni escribe."""
    from app.incident_commander import agente

    settings = servicio.settings
    try:
        return agente.ejecutar(servicio, run_id, settings)
    except RunNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except cliente.LlmNoConfigurado as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except cliente.CuotaExcedida as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.reintentar_en)},
        ) from exc
    except agente.AgentePolitica as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"detail": str(exc), "traza": exc.traza},
        ) from exc
    except (agente.AgenteLimitePasos, agente.AgentePresupuesto) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    except (cliente.LlmUpstreamError, cliente.LlmRespuestaInvalida) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
