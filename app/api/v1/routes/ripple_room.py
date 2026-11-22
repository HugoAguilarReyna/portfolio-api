"""API de «The Ripple Room»: perspectivas consultivas de los roles.

Una sola llamada al modelo devuelve una perspectiva por rol del escenario. No se
simulan varios agentes independientes y el motor determinista no cambia: el
resultado que se envía es el que ya calculó la demo, y la respuesta se etiqueta
como simulación.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

from app.analisis import ripple_room as analisis
from app.api.deps import SettingsDep
from app.api.v1.routes.comun_llm import ejecutar_consulta

router = APIRouter(prefix="/ripple-room", tags=["The Ripple Room · suministro"])


class Escenario(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    nombre: str = Field(min_length=1, max_length=120)
    descripcion: str = Field(default="", max_length=400)


class Resultado(BaseModel):
    service: float = Field(ge=0, le=100)
    shortage: float = Field(ge=0, le=100)
    backlog: float = Field(ge=0, le=10_000)
    cost: float = Field(ge=0, le=10_000_000)


class Rol(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    nombre: str = Field(min_length=1, max_length=80)
    interes: str = Field(default="", max_length=200)


class Plan(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    nombre: str = Field(default="", max_length=80)


class PeticionAnalisis(BaseModel):
    escenario: Escenario
    resultado: Resultado
    roles: list[Rol] = Field(min_length=1, max_length=6)
    plan: Plan

    @model_validator(mode="after")
    def _roles_unicos(self) -> "PeticionAnalisis":
        ids = [rol.id for rol in self.roles]
        if len(set(ids)) != len(ids):
            raise ValueError("Los roles no pueden repetir id.")
        return self


@router.get("/health", summary="Estado de la consulta consultiva")
def health(settings: SettingsDep) -> dict:
    from app.llm import cliente

    return {
        "ok": True,
        "demo": "The Ripple Room · suministro",
        "llm": {"configured": cliente.configurado(settings), "mode": "consulta-json-validada"},
        "llm_executes": False,
        "una_llamada": True,
        "posturas_permitidas": list(analisis.POSTURAS),
    }


@router.post(
    "/analisis",
    summary="Una perspectiva consultiva por rol, en una sola llamada",
)
def analizar_escenario(
    peticion: PeticionAnalisis, settings: SettingsDep
) -> dict:
    contexto = peticion.model_dump()
    return ejecutar_consulta(lambda: analisis.analizar(settings, contexto))
