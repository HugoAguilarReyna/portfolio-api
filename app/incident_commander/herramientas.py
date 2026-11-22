"""Herramientas de solo lectura del Data Incident Commander.

Cada herramienta tiene contrato Pydantic y se valida por nombre y argumentos
antes de ejecutarse. Ninguna escribe: no aprueban, no simulan reparaciones, no
cambian el estado del run ni tocan sistemas externos. `aprobar` y la simulación
en memoria siguen siendo exclusivos de la decisión humana.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.incident_commander import service as dic


class SinArgumentos(BaseModel):
    """Estas herramientas no aceptan argumentos."""

    model_config = ConfigDict(extra="forbid")


class ConsultaValidaciones(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str | None = Field(default=None, max_length=32)


class ConsultaPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=3, max_length=32)


@dataclass(frozen=True)
class Herramienta:
    nombre: str
    descripcion: str
    modelo: type[BaseModel]
    ejecutar: Callable[[dic.IncidentCommander, str, BaseModel], dict[str, Any]]

    def esquema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.nombre,
                "description": self.descripcion,
                "parameters": self.modelo.model_json_schema(),
            },
        }


class ArgumentosInvalidos(ValueError):
    """Los argumentos no cumplen el contrato de la herramienta."""


class HerramientaDesconocida(ValueError):
    """El modelo pidió una herramienta que no existe en el catálogo."""


def _estado(servicio: dic.IncidentCommander, run_id: str, _args: BaseModel) -> dict[str, Any]:
    run = servicio._run(run_id)
    return {
        "run_id": run["run_id"],
        "escenario": run["scenario_id"],
        "estado": run["status"],
        "aprobado": "approval" in run,
        "escrituras_externas": int(run.get("external_writes", 0)),
    }


def _validaciones(servicio: dic.IncidentCommander, run_id: str, args: BaseModel) -> dict[str, Any]:
    run = servicio._run(run_id)
    checks = servicio._evaluate(run["working_copy"])
    return {
        "run_id": run["run_id"],
        "fallidas": [check["id"] for check in checks if not check["passed"]],
        "aprobadas": sum(1 for check in checks if check["passed"]),
        "total": len(checks),
    }


def _matriz(servicio: dic.IncidentCommander, _run_id: str, _args: BaseModel) -> dict[str, Any]:
    return {"criterios": servicio._matrix()}


def _catalogo(servicio: dic.IncidentCommander, _run_id: str, _args: BaseModel) -> dict[str, Any]:
    return {
        "permitidos": list(dic.ALLOWED_PLANS),
        "bloqueados": list(dic.BLOCKED_PLANS),
        "planes": [servicio._plan_summary(plan) for plan in dic.PLAN_META],
    }


def _snapshot(servicio: dic.IncidentCommander, _run_id: str, _args: BaseModel) -> dict[str, Any]:
    return servicio.snapshot_summary()


HERRAMIENTAS: dict[str, Herramienta] = {
    herramienta.nombre: herramienta
    for herramienta in (
        Herramienta("estado_run", "Estado y aprobación del run (solo lectura).", SinArgumentos, _estado),
        Herramienta(
            "validaciones",
            "Resultado de las validaciones del run sobre la copia de trabajo.",
            ConsultaValidaciones,
            _validaciones,
        ),
        Herramienta("matriz_planes", "Matriz de criterios por plan (solo lectura).", SinArgumentos, _matriz),
        Herramienta("catalogo_planes", "Planes permitidos y bloqueados por política.", SinArgumentos, _catalogo),
        Herramienta("snapshot", "Metadatos del snapshot fuente y su digest.", SinArgumentos, _snapshot),
    )
}


def esquemas() -> list[dict[str, Any]]:
    return [herramienta.esquema() for herramienta in HERRAMIENTAS.values()]


def ejecutar(
    servicio: dic.IncidentCommander, run_id: str, nombre: str, argumentos: str | dict[str, Any] | None
) -> dict[str, Any]:
    """Valida nombre y argumentos y ejecuta. Nunca escribe."""
    herramienta = HERRAMIENTAS.get(nombre)
    if herramienta is None:
        raise HerramientaDesconocida(f"Herramienta no permitida: {nombre}")
    try:
        datos = argumentos if isinstance(argumentos, dict) else json.loads(argumentos or "{}")
    except json.JSONDecodeError as exc:
        raise ArgumentosInvalidos("Los argumentos no son JSON válido.") from exc
    try:
        validos = herramienta.modelo.model_validate(datos)
    except ValidationError as exc:
        raise ArgumentosInvalidos(f"Argumentos inválidos para {nombre}.") from exc
    return herramienta.ejecutar(servicio, run_id, validos)
