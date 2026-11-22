"""Bucle de agente con herramientas de solo lectura para Incident Commander.

Máximo 6 pasos y presupuesto de tokens configurable. El modelo solo puede llamar
a las herramientas del catálogo; cualquier nombre, argumento o acción fuera de él
se rechaza y queda en la traza. No aprueba, no escribe y no simula reparaciones:
la decisión sigue siendo humana y la simulación ocurre en `aprobar`.
"""

from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.incident_commander import herramientas as catalogo
from app.incident_commander import service as dic
from app.llm import cliente

SISTEMA = (
    "Eres un agente de diagnóstico de calidad de datos. Tienes herramientas de "
    "SOLO LECTURA; úsalas para reunir evidencia antes de responder. No puedes "
    "aprobar incidentes, cambiar estados ni ejecutar remediaciones. Cuando tengas "
    "la evidencia, responde SOLO con JSON: "
    '{"recommendation":"quarantine|backfill|fullreload","justification":"...",'
    '"evidence":["..."],"risks":["..."]}. '
    "Solo puedes recomendar quarantine, backfill o fullreload; «forward» está "
    "bloqueado. Tus recomendaciones son consultivas. Responde en español."
)


class AgenteLimitePasos(RuntimeError):
    """Se alcanzó el máximo de pasos sin respuesta final."""


class AgentePresupuesto(RuntimeError):
    """Se agotó el presupuesto de tokens del agente."""


class AgentePolitica(RuntimeError):
    """El modelo intentó una herramienta, argumento o acción fuera de política."""

    def __init__(self, mensaje: str, *, traza: list[dict[str, Any]]) -> None:
        super().__init__(mensaje)
        self.traza = traza


def _traza_paso(paso: int, tipo: str, **datos: Any) -> dict[str, Any]:
    """Entrada de traza acotada: sin prompts completos ni credenciales."""
    entrada = {"paso": paso, "tipo": tipo}
    for clave, valor in datos.items():
        if isinstance(valor, str):
            entrada[clave] = valor[:160]
        elif isinstance(valor, (int, float, bool)) or valor is None:
            entrada[clave] = valor
        else:
            entrada[clave] = str(valor)[:160]
    return entrada


def ejecutar(
    servicio: dic.IncidentCommander,
    run_id: str,
    settings: Settings,
    *,
    max_pasos: int | None = None,
    presupuesto_tokens: int | None = None,
) -> dict[str, Any]:
    run = servicio._run(run_id)  # RunNotFound si no existe
    pasos_maximos = int(max_pasos or settings.llm_agente_max_pasos)
    presupuesto = int(presupuesto_tokens or settings.llm_agente_presupuesto_tokens)
    if not cliente.configurado(settings):
        raise cliente.LlmNoConfigurado(
            "La consulta a OpenAI no está configurada en este despliegue."
        )

    contexto = servicio._contexto_consulta(run)
    mensajes: list[dict[str, Any]] = [
        {"role": "system", "content": SISTEMA},
        {"role": "user", "content": cliente.recortar(contexto)},
    ]
    traza: list[dict[str, Any]] = []
    consumido = 0
    intentos_salida = 0

    for paso in range(1, pasos_maximos + 1):
        sobre = cliente.solicitar_chat(
            settings,
            mensajes,
            herramientas=catalogo.esquemas(),
            max_tokens=min(int(settings.llm_max_tokens), 300),
        )
        uso = cliente.uso(sobre)
        if uso and isinstance(uso.get("total_tokens"), int):
            consumido += int(uso["total_tokens"])
        if consumido > presupuesto:
            raise AgentePresupuesto(
                "El agente agotó su presupuesto de tokens sin cerrar la consulta."
            )

        respuesta = cliente.mensaje(sobre)
        llamadas = respuesta.get("tool_calls") or []
        if llamadas:
            for llamada in llamadas:
                funcion = (llamada.get("function") or {}) if isinstance(llamada, dict) else {}
                nombre = str(funcion.get("name") or "")
                argumentos = funcion.get("arguments")
                if nombre not in catalogo.HERRAMIENTAS:
                    traza.append(_traza_paso(paso, "rechazo", herramienta=nombre, motivo="fuera_de_catalogo"))
                    raise AgentePolitica(
                        f"El modelo pidió una herramienta no permitida: {nombre}", traza=traza
                    )
                try:
                    resultado = catalogo.ejecutar(servicio, run_id, nombre, argumentos)
                except catalogo.ArgumentosInvalidos as exc:
                    traza.append(_traza_paso(paso, "rechazo", herramienta=nombre, motivo="argumentos_invalidos"))
                    raise AgentePolitica(str(exc), traza=traza) from exc
                traza.append(_traza_paso(paso, "herramienta", herramienta=nombre))
                mensajes.append({"role": "assistant", "content": "", "tool_calls": [llamada]})
                mensajes.append(
                    {
                        "role": "tool",
                        "tool_call_id": llamada.get("id", f"call-{paso}"),
                        "content": json.dumps(resultado, ensure_ascii=False)[:2000],
                    }
                )
            continue

        contenido = str(respuesta.get("content") or "").strip()
        if not contenido:
            raise cliente.LlmUpstreamError("El proveedor devolvió una respuesta vacía.")
        try:
            plan = dic.validar_recomendacion(contenido)
        except cliente.LlmRespuestaInvalida:
            intentos_salida += 1
            traza.append(_traza_paso(paso, "salida_invalida"))
            if intentos_salida >= 2:
                raise
            mensajes.append(
                {
                    "role": "user",
                    "content": "La respuesta no cumplió el esquema. Devuelve solo el JSON pactado.",
                }
            )
            continue

        traza.append(_traza_paso(paso, "final", plan=plan["plan"]))
        return {
            "run_id": run_id,
            "modo": "openai-tools",
            "modelo": str(settings.llm_model),
            "recomendacion": {
                "plan": plan["plan"],
                "name": plan["plan_name"],
                "allowed": True,
                "forward_blocked": True,
            },
            "justificacion": plan["justification"],
            "evidencia": plan["evidence"],
            "riesgos": plan["risks"],
            "traza": traza,
            "pasos": paso,
            "presupuesto_tokens": presupuesto,
            "tokens_consumidos": consumido,
            "herramientas_disponibles": sorted(catalogo.HERRAMIENTAS),
            "solo_lectura": True,
            "advisory_only": True,
            "approved": False,
            "executed": False,
            "external_writes": 0,
        }

    raise AgenteLimitePasos(
        f"El agente no cerró la consulta en {pasos_maximos} pasos; no se devuelve recomendación."
    )
