"""API de «DockMind»: explicación consultiva del plan de compra.

El pronóstico, las restricciones y el plan salen del motor local de la demo. Esta
ruta recibe esas cifras ya calculadas y pide al modelo que las explique y sugiera
opciones; no recalcula, no ordena compras y no modifica inventario.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

from app.analisis import dockmind as analisis
from app.api.deps import SettingsDep
from app.api.v1.routes.comun_llm import ejecutar_consulta

router = APIRouter(prefix="/dockmind", tags=["DockMind · demanda"])


class Escenario(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    nombre: str = Field(min_length=1, max_length=120)
    parametros: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _parametros_acotados(self) -> "Escenario":
        if len(self.parametros) > 12:
            raise ValueError("«parametros» admite como máximo 12 claves.")
        return self


class Sku(BaseModel):
    sku: str = Field(min_length=1, max_length=40)
    nombre: str = Field(default="", max_length=120)
    categoria: str = Field(default="", max_length=60)
    lead: int = Field(ge=0, le=365)
    pack: int = Field(ge=1, le=10_000)
    stock: int = Field(ge=0, le=10_000_000)
    precio: float = Field(ge=0, le=1_000_000)
    costo: float = Field(ge=0, le=1_000_000)


class Pronostico(BaseModel):
    metodo: str = Field(min_length=1, max_length=60)
    wape: float = Field(ge=0, le=5)
    mae: float = Field(ge=0, le=1_000_000)
    horizonte: int = Field(ge=0, le=520)


class LineaCompra(BaseModel):
    semana: int = Field(ge=0, le=520)
    cantidad: int = Field(ge=0, le=10_000_000)
    costo: float = Field(ge=0, le=100_000_000)


class Restricciones(BaseModel):
    presupuesto: float = Field(ge=0, le=1_000_000_000)
    buffer_dias: int = Field(ge=0, le=365)


class Metricas(BaseModel):
    """Indicadores que la demo ya calculó (se envían tal cual, sin recalcular)."""

    valores: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _acotadas(self) -> "Metricas":
        if len(self.valores) > 12:
            raise ValueError("«metricas» admite como máximo 12 indicadores.")
        for nombre, valor in self.valores.items():
            if len(nombre) > 40:
                raise ValueError("Los nombres de métrica admiten 40 caracteres.")
            if valor != valor or valor in (float("inf"), float("-inf")):
                raise ValueError("Los indicadores deben ser números finitos.")
        return self


class PeticionAnalisis(BaseModel):
    escenario: Escenario
    sku: Sku
    pronostico: Pronostico
    plan_compra: list[LineaCompra] = Field(default_factory=list, max_length=26)
    restricciones: Restricciones
    metricas: Metricas


@router.get("/health", summary="Estado de la consulta consultiva")
def health(settings: SettingsDep) -> dict:
    from app.llm import cliente

    return {
        "ok": True,
        "demo": "DockMind · demanda",
        "llm": {"configured": cliente.configurado(settings), "mode": "consulta-json-validada"},
        "llm_executes": False,
        "opciones_permitidas": list(analisis.OPCIONES),
    }


@router.post(
    "/analisis",
    summary="Explica el plan calculado y propone opciones (no ordena compras)",
)
def analizar_plan(
    peticion: PeticionAnalisis, settings: SettingsDep
) -> dict:
    contexto = peticion.model_dump()
    return ejecutar_consulta(lambda: analisis.analizar(settings, contexto))
