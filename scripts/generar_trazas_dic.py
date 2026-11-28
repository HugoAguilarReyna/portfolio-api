"""Genera trazas sanitizadas de corridas simuladas del agente DIC.

No llama a un proveedor real ni consume cuotas: un guion determinista ocupa el
lugar del modelo y devuelve las llamadas pactadas para cada incidente. El archivo
de salida conserva solo eventos, herramientas, recomendación y errores de política;
no contiene prompts, mensajes internos, credenciales ni filas crudas.

    python scripts/generar_trazas_dic.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app.config import Settings  # noqa: E402
from app.incident_commander import agente  # noqa: E402
from app.incident_commander.service import IncidentCommander  # noqa: E402
from app.llm import cliente, limites  # noqa: E402


def ajustes() -> Settings:
    return Settings(
        _env_file=None,
        llm_base_url="https://proveedor-simulado.invalid/v1",
        llm_api_key="clave-ficticia-no-usada",
        llm_model="mock-traza-v1",
        llm_agente_max_pasos=6,
        llm_agente_max_segundos=60,
        llm_agente_presupuesto_tokens=23_000,
    )


def tool_call(nombre: str, argumentos: dict[str, Any] | None = None, *, identificador: str = "call-1") -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": identificador,
                            "type": "function",
                            "function": {
                                "name": nombre,
                                "arguments": json.dumps(argumentos or {}),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"total_tokens": 120},
    }


def final(plan: str, evidencia: list[str]) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "recommendation": plan,
                            "justification": "La evidencia consultada sostiene la recomendación.",
                            "evidence": evidencia,
                            "risks": ["La aprobación humana sigue siendo obligatoria."],
                        }
                    )
                }
            }
        ],
        "usage": {"total_tokens": 120},
    }


CASOS = (
    {
        "id": "missing-ruta-perfil-linaje",
        "escenario": "missing",
        "respuestas": [
            tool_call("perfil_copia"),
            tool_call("linaje"),
            final(
                "quarantine",
                ["perfil_copia: falta el periodo PIB 2025.", "linaje: activos aguas abajo identificados."],
            ),
        ],
        "explica": "El perfil revela el faltante y el modelo consulta linaje antes de recomendar cuarentena.",
    },
    {
        "id": "schema-ruta-perfil-validaciones",
        "escenario": "schema",
        "respuestas": [
            tool_call("perfil_copia"),
            tool_call("validaciones"),
            final(
                "fullreload",
                ["perfil_copia: indicator_code no conserva el tipo esperado.", "validaciones: el contrato falla."],
            ),
        ],
        "explica": "El tipo alterado del perfil cambia la siguiente consulta a validaciones; no reutiliza la ruta de missing.",
    },
    {
        "id": "evidencia-insuficiente",
        "escenario": "missing",
        "respuestas": [final("quarantine", ["validaciones: no se consultó la herramienta."])],
        "explica": "El modelo intenta cerrar sin herramientas; el servidor detiene la corrida.",
    },
    {
        "id": "bloqueo-herramienta-prohibida",
        "escenario": "missing",
        "respuestas": [tool_call("escribir_tabla")],
        "explica": "El catálogo cerrado rechaza una acción de escritura antes de ejecutarla.",
    },
    {
        "id": "limite-presupuesto",
        "escenario": "duplicate",
        "respuestas": [
            tool_call("estado_run"),
            tool_call("estado_run"),
            tool_call("estado_run"),
            tool_call("estado_run"),
            tool_call("estado_run"),
            tool_call("estado_run"),
        ],
        "presupuesto": 2_300,
        "explica": "El presupuesto detiene la corrida después de la evidencia disponible; no hay reparación.",
    },
)


def _resultado(servicio: IncidentCommander, caso: dict[str, Any], config: Settings) -> dict[str, Any]:
    run = servicio.crear_run(caso["escenario"])
    guion = list(caso["respuestas"])

    def responder(*_args, **_kwargs):
        return guion.pop(0)

    with patch.object(cliente, "post_chat", responder), patch.object(
        limites,
        "reservar_run",
        lambda *_args, **_kwargs: {"visitante_del_dia": 1},
    ):
        try:
            resultado = agente.ejecutar(
                servicio,
                run["run_id"],
                config,
                presupuesto_tokens=caso.get("presupuesto"),
            )
        except (agente.AgentePolitica, agente.AgentePresupuesto, agente.AgenteLimitePasos, agente.AgenteTiempo) as exc:
            return {
                "caso": caso["id"],
                "run_id": run["run_id"],
                "escenario": caso["escenario"],
                "modo": "simulado",
                "modelo": "mock-traza-v1",
                "estado": "detenido",
                "error": type(exc).__name__,
                "mensaje": str(exc)[:240],
                "traza": exc.traza,
                "eventos": exc.eventos,
                "recomendacion": None,
                "explica": caso["explica"],
            }

    return {
        "caso": caso["id"],
        "run_id": run["run_id"],
        "escenario": caso["escenario"],
        "modo": "simulado",
        "modelo": "mock-traza-v1",
        "estado": resultado.get("estado"),
        "motivo_parada": resultado.get("motivo_parada"),
        "herramientas_usadas": resultado.get("herramientas_usadas"),
        "traza": resultado.get("traza"),
        "eventos": resultado.get("eventos"),
        "recomendacion": resultado.get("recomendacion"),
        "justificacion": resultado.get("justificacion"),
        "evidencia": resultado.get("evidencia"),
        "tokens_consumidos": resultado.get("tokens_consumidos"),
        "presupuesto_tokens": resultado.get("presupuesto_tokens"),
        "external_writes": resultado.get("external_writes"),
        "explica": caso["explica"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera trazas sanitizadas del DIC.")
    parser.add_argument(
        "--salida",
        type=Path,
        default=RAIZ / "docs" / "trazas" / "agente-2026-11-27-simulado.json",
    )
    args = parser.parse_args(argv)
    config = ajustes()
    servicio = IncidentCommander(config)
    casos = [_resultado(servicio, caso, config) for caso in CASOS]
    documento = {
        "fecha": "2026-11-27",
        "conjunto": "trazas sintéticas para revisión",
        "proveedor": "simulado",
        "modelo": "mock-traza-v1",
        "nota": (
            "El modelo está guionizado para producir rutas distintas. No es una corrida real "
            "ni mide calidad de un LLM; mide el contrato del grafo, la elección observable y las guardas."
        ),
        "casos": casos,
    }
    args.salida.parent.mkdir(parents=True, exist_ok=True)
    args.salida.write_text(json.dumps(documento, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"trazas en {args.salida}")
    for caso in casos:
        print(f"{caso['caso']}: {caso['estado']} · herramientas={caso.get('herramientas_usadas') or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
