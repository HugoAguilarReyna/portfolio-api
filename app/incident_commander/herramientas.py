"""Herramientas de solo lectura del Data Incident Commander.

Cada herramienta tiene contrato Pydantic y se valida por nombre y argumentos
antes de ejecutarse. Ninguna escribe: no aprueban, no simulan reparaciones, no
cambian el estado del run ni tocan sistemas externos. `aprobar` y la simulación
en memoria siguen siendo exclusivos de la decisión humana.

El catálogo incorpora perfilado de la copia y linaje declarado, además de las
consultas de estado, validaciones, matriz, catálogo y snapshot. El agente elige
entre ellas según la evidencia; no recibe filas crudas ni credenciales.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Literal

from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.incident_commander import service as dic

IndicadorLinaje = Literal["NY.GDP.MKTP.KD.ZG", "FP.CPI.TOTL.ZG", "SL.UEM.TOTL.ZS"]


class SinArgumentos(BaseModel):
    """Estas herramientas no aceptan argumentos."""

    model_config = ConfigDict(extra="forbid")


class ConsultaValidaciones(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str | None = Field(default=None, max_length=32)


class ConsultaPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=3, max_length=32)


class ConsultaLinaje(BaseModel):
    model_config = ConfigDict(extra="forbid")

    indicador: IndicadorLinaje = "NY.GDP.MKTP.KD.ZG"


@dataclass(frozen=True)
class Herramienta:
    nombre: str
    descripcion: str
    modelo: type[BaseModel]
    ejecutar: Callable[[dic.IncidentCommander, str, BaseModel], dict[str, Any]]

    def esquema(self) -> dict[str, Any]:
        # LangChain adapta el modelo Pydantic al esquema de función que consume
        # el proveedor. El nombre y la descripción del catálogo son la autoridad;
        # el esquema generado solo describe argumentos tipados.
        esquema = convert_to_openai_tool(self.modelo)
        funcion = esquema.setdefault("function", {})
        funcion["name"] = self.nombre
        funcion["description"] = self.descripcion
        return esquema


class ArgumentosInvalidos(ValueError):
    """Los argumentos no cumplen el contrato de la herramienta."""


class HerramientaDesconocida(ValueError):
    """El modelo pidió una herramienta que no existe en el catálogo."""


def _envolver(
    nombre: str,
    resumen: str,
    datos: dict[str, Any],
    *,
    evidencia_id: str | None = None,
) -> dict[str, Any]:
    """Resultado acotado y citables: el modelo ve un resumen, no filas crudas."""
    return {
        "herramienta": nombre,
        "evidencia_id": evidencia_id or nombre,
        "resumen": resumen[:320],
        "datos": datos,
        "solo_lectura": True,
    }


def _estado(servicio: dic.IncidentCommander, run_id: str, _args: BaseModel) -> dict[str, Any]:
    run = servicio._run(run_id)
    return _envolver(
        "estado_run",
        f"Run {run['run_id']} en estado {run['status']}.",
        {
            "run_id": run["run_id"],
            "escenario": run["scenario_id"],
            "estado": run["status"],
            "aprobado": "approval" in run,
            "escrituras_externas": int(run.get("external_writes", 0)),
        },
    )


def _perfil(servicio: dic.IncidentCommander, run_id: str, _args: BaseModel) -> dict[str, Any]:
    """Resumen agregado de la copia de trabajo; nunca devuelve valores crudos."""
    run = servicio._run(run_id)
    filas = run["working_copy"]
    codigos = Counter(str(fila.get("indicator_code")) for fila in filas)
    columnas = sorted({clave for fila in filas for clave in fila})
    anios_pib = sorted(
        {
            int(fila["year"])
            for fila in filas
            if str(fila.get("indicator_code")) == dic.GDP and isinstance(fila.get("year"), int)
        }
    )
    faltantes = sorted(dic.EXPECTED_YEARS - set(anios_pib))
    llaves = [(fila.get("year"), str(fila.get("indicator_code"))) for fila in filas]
    duplicados = len(llaves) - len(set(llaves))
    tipos = Counter(type(fila.get("indicator_code")).__name__ for fila in filas)
    resumen = (
        f"{len(filas)} filas; {duplicados} llaves duplicadas; "
        f"{len(faltantes)} periodos PIB faltantes; "
        f"{len(codigos)} códigos de indicador."
    )
    return _envolver(
        "perfil_copia",
        resumen,
        {
            "filas": len(filas),
            "columnas": columnas,
            "anios_pib": anios_pib,
            "periodos_pib_faltantes": faltantes,
            "llaves_duplicadas": duplicados,
            "tipos_indicator_code": dict(tipos),
            "codigos": dict(codigos),
        },
    )


def _linaje(servicio: dic.IncidentCommander, _run_id: str, args: BaseModel) -> dict[str, Any]:
    datos = servicio.linaje_summary(getattr(args, "indicador", dic.GDP))
    resumen = (
        f"{datos['dependencias']} activos declarados aguas abajo de "
        f"{datos['indicador']} (fixture de linaje)."
    )
    return _envolver("linaje", resumen, datos)


def _validaciones(servicio: dic.IncidentCommander, run_id: str, args: BaseModel) -> dict[str, Any]:
    run = servicio._run(run_id)
    checks = servicio._evaluate(run["working_copy"])
    fallidas = [check["id"] for check in checks if not check["passed"]]
    aprobadas = sum(1 for check in checks if check["passed"])
    return _envolver(
        "validaciones",
        f"{len(fallidas)} validaciones fallidas de {len(checks)}; {aprobadas} aprobadas.",
        {
            "run_id": run["run_id"],
            "fallidas": fallidas,
            "aprobadas": aprobadas,
            "total": len(checks),
            "checks": [{"id": check["id"], "passed": bool(check["passed"])} for check in checks],
        },
    )


def _matriz(servicio: dic.IncidentCommander, _run_id: str, _args: BaseModel) -> dict[str, Any]:
    criterios = servicio._matrix()
    return _envolver(
        "matriz_planes",
        f"{len(criterios)} criterios comparados para los planes de la política.",
        {"criterios": criterios},
    )


def _catalogo(servicio: dic.IncidentCommander, _run_id: str, _args: BaseModel) -> dict[str, Any]:
    return _envolver(
        "catalogo_planes",
        f"Planes permitidos: {', '.join(dic.ALLOWED_PLANS)}; bloqueados: {', '.join(dic.BLOCKED_PLANS)}.",
        {
            "permitidos": list(dic.ALLOWED_PLANS),
            "bloqueados": list(dic.BLOCKED_PLANS),
            "planes": [servicio._plan_summary(plan) for plan in dic.PLAN_META],
        },
    )


def _snapshot(servicio: dic.IncidentCommander, _run_id: str, _args: BaseModel) -> dict[str, Any]:
    resumen = servicio.snapshot_summary()
    return _envolver(
        "snapshot",
        f"Snapshot {resumen.get('dataset_id')} con {resumen.get('observation_count')} observaciones.",
        {
            "dataset_id": resumen.get("dataset_id"),
            "snapshot_date": resumen.get("snapshot_date"),
            "observation_count": resumen.get("observation_count"),
            "source_digest": resumen.get("source_digest"),
            "period": resumen.get("period"),
            "sources": [
                {
                    "indicator_code": fuente.get("indicator_code"),
                    "title": fuente.get("title"),
                    "url": fuente.get("url"),
                }
                for fuente in (resumen.get("sources") or [])
            ],
        },
    )


HERRAMIENTAS: dict[str, Herramienta] = {
    herramienta.nombre: herramienta
    for herramienta in (
        Herramienta("estado_run", "Estado y aprobación del run (solo lectura).", SinArgumentos, _estado),
        Herramienta(
            "perfil_copia",
            "Perfil agregado de la copia de trabajo: filas, cobertura, códigos, duplicados y tipos. No devuelve valores crudos.",
            SinArgumentos,
            _perfil,
        ),
        Herramienta(
            "linaje",
            "Activos declarados aguas abajo de un indicador; es un linaje fixture de la demo.",
            ConsultaLinaje,
            _linaje,
        ),
        Herramienta(
            "validaciones",
            "Resultado de las validaciones del run sobre la copia de trabajo.",
            ConsultaValidaciones,
            _validaciones,
        ),
        Herramienta("matriz_planes", "Matriz de criterios por plan (solo lectura).", SinArgumentos, _matriz),
        Herramienta("catalogo_planes", "Planes permitidos y bloqueados por política.", SinArgumentos, _catalogo),
        Herramienta("snapshot", "Metadatos del snapshot fuente y sus fuentes.", SinArgumentos, _snapshot),
    )
}


def esquemas() -> list[dict[str, Any]]:
    return [herramienta.esquema() for herramienta in HERRAMIENTAS.values()]


def validar(
    nombre: str, argumentos: str | dict[str, Any] | None
) -> tuple[Herramienta, BaseModel]:
    """Valida nombre y argumentos sin ejecutar; el servidor es la autoridad."""
    herramienta = HERRAMIENTAS.get(nombre)
    if herramienta is None:
        raise HerramientaDesconocida(f"Herramienta no permitida: {nombre}")
    try:
        datos = argumentos if isinstance(argumentos, dict) else json.loads(argumentos or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ArgumentosInvalidos("Los argumentos no son JSON válido.") from exc
    if not isinstance(datos, dict):
        raise ArgumentosInvalidos("Los argumentos deben ser un objeto JSON.")
    try:
        validos = herramienta.modelo.model_validate(datos)
    except ValidationError as exc:
        raise ArgumentosInvalidos(f"Argumentos inválidos para {nombre}.") from exc
    return herramienta, validos


def ejecutar(
    servicio: dic.IncidentCommander, run_id: str, nombre: str, argumentos: str | dict[str, Any] | None
) -> dict[str, Any]:
    """Valida nombre y argumentos y ejecuta. Nunca escribe."""
    herramienta, validos = validar(nombre, argumentos)
    return herramienta.ejecutar(servicio, run_id, validos)
