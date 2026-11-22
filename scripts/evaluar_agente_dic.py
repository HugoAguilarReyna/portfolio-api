"""Evalúa el agente con herramientas sobre incidentes sintéticos.

    python scripts/evaluar_agente_dic.py

Usa un proveedor simulado determinista: no hay red ni coste. Las cifras miden el
arnés y el contrato, no el modelo real.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app.config import Settings  # noqa: E402
from app.evals import dic as evals  # noqa: E402
from app.incident_commander import agente  # noqa: E402
from app.incident_commander.service import IncidentCommander  # noqa: E402
from app.llm import cliente  # noqa: E402


class ProveedorGuiado:
    """Devuelve la secuencia guionizada de cada incidente sintético."""

    def __init__(self, incidentes: list[dict[str, Any]]) -> None:
        self.plan = {fila["id"]: fila for fila in incidentes}
        self.actual: dict[str, Any] | None = None
        self.paso = 0

    def __call__(self, settings, payload, *, timeout=None):
        caso = self.actual
        assert caso is not None
        self.paso += 1
        secuencia = caso["herramientas"]
        if self.paso <= len(secuencia):
            nombre = secuencia[self.paso - 1]
            argumentos = caso.get("argumentos", {}).get(nombre, {})
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": f"call-{self.paso}",
                                    "type": "function",
                                    "function": {"name": nombre, "arguments": json.dumps(argumentos)},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"total_tokens": 120},
            }
        contenido = caso.get("salida") or json.dumps(
            {
                "recommendation": caso["plan"],
                "justification": caso["justificacion"],
                "evidence": caso["evidencia"],
                "risks": caso["riesgos"],
            },
            ensure_ascii=False,
        )
        return {
            "choices": [{"message": {"role": "assistant", "content": contenido}}],
            "usage": {"total_tokens": 120},
        }


def main() -> int:
    incidentes = evals.cargar_incidentes()
    settings = Settings(
        _env_file=None,
        llm_base_url="https://proveedor-simulado.invalid/v1",
        llm_api_key="clave-ficticia-de-evaluacion",
        llm_model="mock-agente-v1",
        llm_consultas_por_minuto=120,
        llm_agente_max_pasos=6,
        llm_agente_presupuesto_tokens=5_000,
    )
    servicio = IncidentCommander(settings)
    proveedor = ProveedorGuiado(incidentes)
    original = cliente.post_chat
    cliente.post_chat = proveedor
    filas: list[dict[str, Any]] = []
    try:
        for caso in incidentes:
            proveedor.actual = caso
            proveedor.paso = 0
            run = servicio.crear_run(caso["escenario"])
            esperadas = set(caso["herramientas"])
            try:
                resultado = agente.ejecutar(servicio, run["run_id"], settings)
                usadas = {
                    entrada["herramienta"]
                    for entrada in resultado["traza"]
                    if entrada.get("tipo") == "herramienta"
                }
                filas.append(
                    {
                        "id": caso["id"],
                        "ok": True,
                        "plan_correcto": resultado["recomendacion"]["plan"] == caso["plan"],
                        "herramientas_ok": usadas == esperadas,
                        "precision": len(usadas & esperadas) / len(usadas) if usadas else None,
                        "recall": len(usadas & esperadas) / len(esperadas) if esperadas else None,
                        "pasos": resultado["pasos"],
                        "politica": not caso.get("violacion", False),
                        "escrituras": resultado["external_writes"],
                    }
                )
            except (agente.AgentePolitica, agente.AgenteLimitePasos, cliente.LlmRespuestaInvalida) as exc:
                filas.append(
                    {
                        "id": caso["id"],
                        "ok": False,
                        "bloqueado": type(exc).__name__,
                        "politica": not caso.get("violacion", False),
                    }
                )
    finally:
        cliente.post_chat = original

    total = len(filas)
    correctos = [fila for fila in filas if fila.get("ok")]
    metricas = {
        "incidentes": total,
        "cerrados": len(correctos),
        "plan_correcto": round(sum(1 for fila in correctos if fila.get("plan_correcto")) / max(1, len(correctos)), 4),
        "herramientas_exactas": round(sum(1 for fila in correctos if fila.get("herramientas_ok")) / max(1, len(correctos)), 4),
        "precision_herramientas": round(
            sum(fila["precision"] for fila in correctos if fila.get("precision") is not None)
            / max(1, sum(1 for fila in correctos if fila.get("precision") is not None)),
            4,
        ),
        "pasos_medios": round(
            sum(fila.get("pasos", 0) for fila in correctos) / max(1, len(correctos)), 3
        ),
        "violaciones_de_politica": sum(
            1 for fila in filas if fila.get("bloqueado") in {"AgentePolitica"}
        ),
        "escrituras_externas": sum(fila.get("escrituras", 0) for fila in filas),
        "baseline_determinista": {"plan": "quarantine", "incidentes": total, "correctos": total},
    }
    resultado = {
        "conjunto": {"version": evals.VERSION, "sintetico": True, "incidentes": total},
        "ejecucion": {
            "fecha": datetime.now(timezone.utc).isoformat(),
            "modo": "simulado",
            "modelo": "mock-agente-v1",
            "proveedor_real": False,
            "max_pasos": settings.llm_agente_max_pasos,
            "presupuesto_tokens": settings.llm_agente_presupuesto_tokens,
        },
        "metricas": metricas,
        "por_incidente": filas,
        "notas": [
            "Proveedor simulado: las cifras miden el contrato del agente, no al modelo real.",
            "El baseline determinista siempre propone cuarentena con 12 validaciones.",
            "Los intentos de herramienta fuera de catálogo y los ciclos sin cierre se bloquean; se registran como violaciones o bloqueos.",
        ],
        "costo": {"usd": "no calculable", "motivo": "proveedor simulado; sin tabla de precios"},
    }
    destino = evals.RESULTADOS / f"agente-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-simulado.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    for clave, valor in metricas.items():
        print(f"{clave}: {valor}")
    print(f"resultados en {destino}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
