"""API de «Kilómetro Cero»: lectura consultiva del escenario activo.

La demo es un simulador determinista y sintético. Esta ruta no recibe la base de
datos ni escribe nada: valida un resumen acotado del escenario, pide al modelo una
recomendación y la devuelve etiquetada como simulada y consultiva. La persona
decide qué aplicar en el simulador.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

from app.analisis import kilometro_cero as analisis
from app.api.deps import CuotaLLMDep, SettingsDep
from app.api.v1.routes.comun_llm import ejecutar_consulta

router = APIRouter(prefix="/kilometro-cero", tags=["Kilómetro Cero · logística"])


class Escenario(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    nombre: str = Field(min_length=1, max_length=120)
    descripcion: str = Field(default="", max_length=400)


class Red(BaseModel):
    envios: int = Field(ge=0, le=100_000)
    a_tiempo: int = Field(ge=0, le=100_000)
    retrasados: int = Field(ge=0, le=100_000)
    en_riesgo: int = Field(ge=0, le=100_000)
    corredores_activos: int = Field(ge=0, le=1_000)


class Corredor(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    origen: str = Field(min_length=1, max_length=80)
    destino: str = Field(min_length=1, max_length=80)
    envios: int = Field(ge=0, le=100_000)
    a_tiempo: int = Field(ge=0, le=100_000)
    retrasados: int = Field(ge=0, le=100_000)
    en_riesgo: int = Field(ge=0, le=100_000)
    km: float = Field(ge=0, le=20_000)


class Incidencia(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    tipo: str = Field(min_length=1, max_length=40)
    detalle: str = Field(min_length=1, max_length=300)
    corredores_afectados: list[str] = Field(default_factory=list, max_length=20)


class PeticionAnalisis(BaseModel):
    escenario: Escenario
    red: Red
    corredores: list[Corredor] = Field(min_length=1, max_length=20)
    incidencias: list[Incidencia] = Field(default_factory=list, max_length=10)
    acciones_disponibles: list[str] = Field(
        default_factory=lambda: list(analisis.ACCIONES), max_length=8
    )

    @model_validator(mode="after")
    def _coherente(self) -> "PeticionAnalisis":
        ids = [fila.id for fila in self.corredores]
        if len(set(ids)) != len(ids):
            raise ValueError("Los corredores no pueden repetir id.")
        fuera = set(self.acciones_disponibles) - set(analisis.ACCIONES)
        if fuera:
            raise ValueError("Hay acciones que el simulador no ofrece.")
        desconocidos = {
            corredor
            for incidencia in self.incidencias
            for corredor in incidencia.corredores_afectados
            if corredor not in set(ids)
        }
        if desconocidos:
            raise ValueError("Una incidencia cita corredores que no existen.")
        return self


@router.get("/health", summary="Estado de la consulta consultiva")
def health(settings: SettingsDep) -> dict:
    from app.llm import cliente

    return {
        "ok": True,
        "demo": "Kilómetro Cero · logística",
        "llm": {"configured": cliente.configurado(settings), "mode": "consulta-json-validada"},
        "llm_executes": False,
        "acciones_permitidas": list(analisis.ACCIONES),
    }


@router.post(
    "/analisis",
    summary="Recomendación consultiva del escenario activo (no despacha nada)",
)
def analizar_escenario(
    peticion: PeticionAnalisis, settings: SettingsDep, cuota: CuotaLLMDep
) -> dict:
    contexto = peticion.model_dump()
    return ejecutar_consulta(lambda: analisis.analizar(settings, contexto))
