"""Configuración opcional de trazas LangSmith para el agente del DIC.

LangGraph y LangChain leen `LANGSMITH_*`/`LANGCHAIN_*` del entorno. Aquí solo se
decide si la corrida lleva metadatos de traza y se evita cualquier valor que no
sea imprescindible: identificador del run, escenario, versión del flujo y avisos
de política. No se envían prompts, resultados de herramientas, credenciales ni
la IP del visitante.

La integración está apagada por omisión. Si las variables no están completas, el
agente funciona igual y no se crea ningún cliente de LangSmith.
"""

from __future__ import annotations

import os
from typing import Any


def _booleano(nombre: str) -> bool:
    return (os.getenv(nombre) or "").strip().lower() in {"1", "true", "yes", "on"}


def habilitado() -> bool:
    """LangSmith solo se activa con la bandera y una clave presentes."""
    bandera = _booleano("LANGSMITH_TRACING") or _booleano("LANGCHAIN_TRACING_V2")
    clave = (os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY") or "").strip()
    return bool(bandera and clave)


def config_traza(*, run_id: str, scenario_id: str, modo: str) -> dict[str, Any]:
    """Config mínima para `graph.invoke`; vacía si LangSmith está apagado.

    Los metadatos son deliberadamente agregados. El estado del grafo ya solo
    contiene resúmenes acotados de las herramientas, nunca filas crudas.
    """
    if not habilitado():
        return {}
    return {
        "run_name": f"dic-{modo}",
        "tags": ["data-incident-commander", "langgraph", "solo-lectura"],
        "metadata": {
            "run_id": run_id,
            "scenario_id": scenario_id,
            "flujo": modo,
            "advisory_only": True,
            "external_writes": 0,
        },
    }


def estado_publico() -> dict[str, str]:
    """Estado que puede mostrarse en una respuesta; nunca incluye la clave."""
    return {"langsmith": "activado" if habilitado() else "desactivado"}
