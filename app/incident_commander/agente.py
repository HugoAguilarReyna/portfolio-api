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
from app.llm import contexto as contexto_llm
from app.llm import limites as limites_llm

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
    """Se agotó el presupuesto de tokens del agente.

    Lleva la traza parcial para que la interfaz muestre lo que sí se hizo
    antes de cerrar: el visitante ya gastó su cupo y no debe quedarse a ciegas.
    """

    def __init__(self, mensaje: str, *, traza: list[dict[str, Any]] | None = None) -> None:
        super().__init__(mensaje)
        self.traza = list(traza or [])


class AgentePolitica(RuntimeError):
    """El modelo intentó una herramienta, argumento o acción fuera de política."""

    def __init__(self, mensaje: str, *, traza: list[dict[str, Any]]) -> None:
        super().__init__(mensaje)
        self.traza = traza


#: Estimación de tokens por carácter usada para el cierre ordenado. Es una
#: cota conservadora (el tokenizador real suele dar menos); se recalcula con el
#: uso que reporta el proveedor después de cada paso.
CHARS_POR_TOKEN = 3.6


def _estimar_tokens(
    mensajes: list[dict[str, Any]], salida_tope: int, fijo_chars: int = 0
) -> int:
    """Estimación del coste del siguiente paso: historial completo + salida tope."""
    # `fijo_chars` son los esquemas de herramientas: viajan en cada petición y no
    # están dentro de `mensajes`, así que también cuentan para el siguiente paso.
    caracteres = int(fijo_chars)
    for mensaje in mensajes:
        contenido = mensaje.get("content")
        if isinstance(contenido, str):
            caracteres += len(contenido)
        llamadas = mensaje.get("tool_calls")
        if llamadas:
            caracteres += len(json.dumps(llamadas, ensure_ascii=False))
    return int(round(caracteres / CHARS_POR_TOKEN)) + int(salida_tope)


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

    # Una sola reserva por run: cuenta 1 solicitud del cupo por IP y del global, y
    # reserva el presupuesto de tokens del run. Si el run falla a mitad, el cupo ya
    # quedó consumido: repetirlo exige cupo nuevo (no hay reintentos gratis).
    limites_llm.reservar_run(
        settings, ip=contexto_llm.ip_actual(), presupuesto_tokens=presupuesto
    )

    contexto_datos = servicio._contexto_consulta(run)
    mensajes: list[dict[str, Any]] = [
        {"role": "system", "content": SISTEMA},
        {"role": "user", "content": cliente.recortar(contexto_datos)},
    ]
    traza: list[dict[str, Any]] = []
    consumido = 0
    intentos_salida = 0

    esquemas_chars = len(json.dumps(catalogo.esquemas(), ensure_ascii=False))

    with contexto_llm.reserva_de_run():
        for paso in range(1, pasos_maximos + 1):
            # Cierre ordenado: si el siguiente paso ya no cabe en el presupuesto,
            # no se hace otra llamada y se devuelve la traza parcial.
            salida_tope = min(int(settings.llm_max_tokens), 300)
            if consumido + _estimar_tokens(mensajes, salida_tope, esquemas_chars) > presupuesto:
                traza.append(_traza_paso(paso, "presupuesto"))
                raise AgentePresupuesto(
                    "El agente alcanzó su presupuesto de tokens; se devuelve la traza "
                    "parcial sin hacer otra llamada.",
                    traza=traza,
                )
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
                traza.append(_traza_paso(paso, "presupuesto"))
                raise AgentePresupuesto(
                    "El agente agotó su presupuesto de tokens sin cerrar la consulta.",
                    traza=traza,
                )

            respuesta = cliente.mensaje(sobre)
            llamadas = respuesta.get("tool_calls") or []
            if llamadas:
                for llamada in llamadas:
                    funcion = (llamada.get("function") or {}) if isinstance(llamada, dict) else {}
                    nombre = str(funcion.get("name") or "")
                    argumentos = funcion.get("arguments")
                    if nombre not in catalogo.HERRAMIENTAS:
                        traza.append(
                            _traza_paso(paso, "rechazo", herramienta=nombre, motivo="fuera_de_catalogo")
                        )
                        raise AgentePolitica(
                            f"El modelo pidió una herramienta no permitida: {nombre}", traza=traza
                        )
                    try:
                        resultado = catalogo.ejecutar(servicio, run_id, nombre, argumentos)
                    except catalogo.ArgumentosInvalidos as exc:
                        traza.append(
                            _traza_paso(paso, "rechazo", herramienta=nombre, motivo="argumentos_invalidos")
                        )
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
