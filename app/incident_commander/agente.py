"""Fachada del agente supervisor de Data Incident Commander.

El agente real vive en `grafo.py` (LangGraph). Este módulo conserva la API que
usan las rutas, las pruebas y la evaluación, y añade el **flujo determinista**
como línea base explícita. El flujo determinista no llama al modelo ni consume
cuotas: ejecuta las mismas herramientas de solo lectura y propone cuarentena,
que es la decisión conservadora del proyecto.

La autoridad sigue acotada: ninguna de las dos variantes aprueba, escribe,
cambia el estado del run ni ejecuta reparaciones. La aprobación humana y la
simulación en memoria se hacen en `service.aprobar`.
"""

from __future__ import annotations

import time
from typing import Any

from app.config import Settings
from app.incident_commander import herramientas as catalogo
from app.incident_commander import observabilidad
from app.incident_commander import service as dic
from app.incident_commander.grafo import (  # noqa: F401 - API pública reexportada
    AgenteLimitePasos,
    AgentePolitica,
    AgentePresupuesto,
    AgenteTiempo,
    ejecutar as ejecutar_grafo,
)

__all__ = [
    "AgenteLimitePasos",
    "AgentePolitica",
    "AgentePresupuesto",
    "AgenteTiempo",
    "ejecutar",
    "ejecutar_determinista",
]


def _evento(
    eventos: list[dict[str, Any]],
    tipo: str,
    *,
    actor: str,
    resumen: str,
    herramienta: str | None = None,
    **extras: Any,
) -> None:
    evento = {
        "seq": len(eventos) + 1,
        "tipo": tipo,
        "actor": actor,
        "resumen": resumen[:240],
        "duracion_ms": 0,
    }
    if herramienta:
        evento["herramienta"] = herramienta
    evento.update(extras)
    eventos.append(evento)


def ejecutar(
    servicio: dic.IncidentCommander,
    run_id: str,
    settings: Settings,
    *,
    max_pasos: int | None = None,
    presupuesto_tokens: int | None = None,
    max_segundos: float | None = None,
) -> dict[str, Any]:
    """Ejecuta el grafo LangGraph con las mismas guardas de cuota que antes."""
    return ejecutar_grafo(
        servicio,
        run_id,
        settings,
        max_pasos=max_pasos,
        presupuesto_tokens=presupuesto_tokens,
        max_segundos=max_segundos,
    )


def ejecutar_determinista(
    servicio: dic.IncidentCommander,
    run_id: str,
) -> dict[str, Any]:
    """Línea base reproducible: herramientas de lectura + cuarentena.

    No consulta al proveedor ni consume cupo. Sirve para comparar la selección
    del agente contra una política conservadora y para mantener la demo
    funcionando si el modelo no está configurado.
    """
    run = servicio._run(run_id)
    inicio = time.monotonic()
    eventos: list[dict[str, Any]] = []
    traza: list[dict[str, Any]] = []
    _evento(
        eventos,
        "entrada",
        actor="sistema",
        resumen=f"Incidente {run['scenario_id']} recibido; flujo determinista sin LLM.",
    )

    evidencia: list[str] = []
    herramientas_usadas: list[str] = []
    for paso, (nombre, argumentos) in enumerate(
        (
            ("perfil_copia", {}),
            ("linaje", {}),
            ("validaciones", {}),
        ),
        start=1,
    ):
        resultado = catalogo.ejecutar(servicio, run_id, nombre, argumentos)
        herramientas_usadas.append(nombre)
        evidencia.append(f"{nombre}: {resultado['resumen']}")
        traza.append({"paso": paso, "tipo": "herramienta", "herramienta": nombre})
        _evento(
            eventos,
            "herramienta",
            actor="herramienta",
            resumen=resultado["resumen"],
            herramienta=nombre,
            evidencia_id=resultado.get("evidencia_id", nombre),
            solo_lectura=True,
            duracion_ms=int((time.monotonic() - inicio) * 1000),
        )
        _evento(
            eventos,
            "resultado",
            actor="herramienta",
            resumen="Resultado agregado a la evidencia del flujo determinista.",
            herramienta=nombre,
            evidencia_id=resultado.get("evidencia_id", nombre),
            duracion_ms=int((time.monotonic() - inicio) * 1000),
        )

    plan = "quarantine"
    _evento(
        eventos,
        "validacion",
        actor="servidor",
        resumen="Política aplicada: cuarentena permitida, imputación bloqueada.",
        estado_validacion="aprobada",
        duracion_ms=int((time.monotonic() - inicio) * 1000),
    )
    _evento(
        eventos,
        "recomendacion",
        actor="servidor",
        resumen=f"Recomendación determinista: {plan}.",
        plan=plan,
        duracion_ms=int((time.monotonic() - inicio) * 1000),
    )
    _evento(
        eventos,
        "revision_humana",
        actor="humano",
        resumen="Recomendación pendiente de revisión humana; no se ejecutó nada.",
        estado_revision="pendiente",
        duracion_ms=int((time.monotonic() - inicio) * 1000),
    )
    traza.append({"paso": 4, "tipo": "validacion", "plan": plan})
    traza.append({"paso": 4, "tipo": "final", "plan": plan})

    return {
        "run_id": run_id,
        "modo": "determinista",
        "flujo": "determinista",
        "modelo": None,
        "estado": "completado",
        "motivo_parada": None,
        "recomendacion": {
            "plan": plan,
            "name": dic.PLAN_META[plan]["name"],
            "allowed": True,
            "forward_blocked": True,
        },
        "justificacion": (
            "La copia se aísla y se recarga desde la fuente; la imputación de "
            "valores está bloqueada por la política."
        ),
        "evidencia": evidencia,
        "riesgos": [
            "La decisión final y la simulación de la reparación siguen requiriendo aprobación humana."
        ],
        "traza": traza,
        "eventos": eventos,
        "pasos": 1,
        "llamadas_modelo": 0,
        "llamadas_herramientas": len(herramientas_usadas),
        "tokens_consumidos": 0,
        "presupuesto_tokens": 0,
        "herramientas_usadas": herramientas_usadas,
        "herramientas_disponibles": sorted(catalogo.HERRAMIENTAS),
        "solo_lectura": True,
        "advisory_only": True,
        "approved": False,
        "executed": False,
        "external_writes": 0,
        "human_approval": "pending",
        "observabilidad": observabilidad.estado_publico(),
        "evidencia_insuficiente": False,
        "evidencia_consultada": [
            {"herramienta": nombre, "resumen": evidencia[indice]}
            for indice, nombre in enumerate(herramientas_usadas)
        ],
    }
