"""Modelos de la telemetría anónima del portafolio.

La telemetría solo guarda contadores agregados. Ni la IP ni el `User-Agent` se
persisten: la unicidad diaria se resuelve con una huella HMAC irreversible sin
la clave del despliegue. No se usan cookies ni identificadores de sesión.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: Un identificador de proyecto o área: alfanumérico y guiones, de 2 a 64.
PATRON_SLUG = r"^[a-zA-Z0-9-]{2,64}$"


class _Modelo(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VistaProyecto(_Modelo):
    """Cuerpo de `POST /api/v1/visitas/proyecto`."""

    proyecto_id: str = Field(
        min_length=2,
        max_length=64,
        pattern=PATRON_SLUG,
        description="Identificador del proyecto explorado (alfanumérico y guiones).",
    )
    area_id: str | None = Field(
        default=None,
        min_length=2,
        max_length=64,
        pattern=PATRON_SLUG,
        description="Área a la que pertenece el proyecto; opcional.",
    )


class RegistroVisita(_Modelo):
    """Respuesta de `POST /api/v1/visitas/registrar`."""

    visitas_totales: int
    visitantes_unicos: int
    contabilizada: bool = Field(
        description="False cuando el User-Agent corresponde a un bot o monitoreo."
    )


class VistasProyecto(_Modelo):
    """Respuesta de `POST /api/v1/visitas/proyecto`."""

    ok: bool
    proyecto_id: str
    vistas_proyecto: int


class ProyectoEnRanking(_Modelo):
    """Una entrada del ranking de proyectos más explorados."""

    proyecto_id: str
    vistas: int


class EstadisticasVisitas(_Modelo):
    """Respuesta de `GET /api/v1/visitas`."""

    visitas_totales: int
    visitantes_unicos: int
    top_proyectos: list[ProyectoEnRanking]
