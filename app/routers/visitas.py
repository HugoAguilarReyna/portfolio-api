"""Telemetría anónima del portafolio.

Tres rutas, todas bajo ``/api/v1``:

* ``POST /visitas/registrar``: cuenta una visita y un visitante único diario.
* ``POST /visitas/proyecto``: cuenta la exploración de un proyecto concreto.
* ``GET  /visitas``: devuelve los contadores y el ranking de proyectos.

Privacidad: la IP se resuelve con el middleware compartido (solo confía en
``X-Forwarded-For``/``X-Real-IP`` si la conexión viene de ``LLM_PROXIES_CONFIABLES``)
y **nunca se persiste**. La unicidad diaria usa un HMAC con
``LLM_CUOTA_HMAC_KEY``; sin esa clave las rutas que escriben fallan cerrado (503)
porque no se puede distinguir a los visitantes sin guardar la IP.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import DatabaseDep, SettingsDep
from app.config import Settings
from app.llm import contexto, limites
from app.models.visitas import (
    EstadisticasVisitas,
    RegistroVisita,
    VistaProyecto,
    VistasProyecto,
)
from app.services.visitas_service import ServicioVisitas

router = APIRouter(prefix="/visitas", tags=["Telemetría anónima"])


def get_servicio_visitas(db: DatabaseDep, settings: SettingsDep) -> ServicioVisitas:
    return ServicioVisitas(db.db, settings)


VisitasDep = Annotated[ServicioVisitas, Depends(get_servicio_visitas)]


def _ip_visitante(request: Request, settings: Settings) -> str:
    """IP resuelta por el middleware; se recalcula si no hay contexto.

    El middleware ``IPVisitanteMiddleware`` ya aplica
    :func:`app.llm.limites.ip_real` en cada petición. El respaldo existe para las
    pruebas de la ruta y para no depender del orden de los middlewares.
    """
    resuelta = contexto.ip_actual()
    if resuelta:
        return resuelta
    host = request.client.host if request.client else None
    return limites.ip_real(host, request.headers, settings)


def _sin_clave() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "La telemetría anónima no está configurada (falta "
            "LLM_CUOTA_HMAC_KEY): no se registran visitas para no guardar la IP "
            "en claro."
        ),
    )


@router.post(
    "/registrar",
    response_model=RegistroVisita,
    summary="Registra una visita y calcula el visitante único diario",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Falta LLM_CUOTA_HMAC_KEY: no se registra la visita."
        }
    },
)
def registrar_visita(
    request: Request,
    servicio: VisitasDep,
    settings: SettingsDep,
) -> RegistroVisita:
    """Cuenta la visita; los bots y monitores no incrementan nada."""
    user_agent = request.headers.get("user-agent")
    try:
        datos = servicio.registrar(
            ip=_ip_visitante(request, settings),
            user_agent=user_agent,
        )
    except limites.LimitesNoConfigurados as exc:
        raise _sin_clave() from exc
    return RegistroVisita(**datos)


@router.post(
    "/proyecto",
    response_model=VistasProyecto,
    summary="Registra la exploración de un proyecto",
)
def registrar_proyecto(
    datos: VistaProyecto,
    request: Request,
    servicio: VisitasDep,
) -> VistasProyecto:
    """Incrementa la vista del proyecto (y del área, si se indica)."""
    resultado = servicio.registrar_proyecto(
        proyecto_id=datos.proyecto_id,
        area_id=datos.area_id,
        user_agent=request.headers.get("user-agent"),
    )
    return VistasProyecto(**resultado)


@router.get(
    "",
    response_model=EstadisticasVisitas,
    summary="Contadores globales y ranking de proyectos",
)
def estadisticas(servicio: VisitasDep) -> EstadisticasVisitas:
    """Lee los contadores sin incrementar nada."""
    return EstadisticasVisitas(**servicio.estado())
