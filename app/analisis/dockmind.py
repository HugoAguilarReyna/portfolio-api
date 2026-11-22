"""DockMind · explicación consultiva del plan de compra simulado.

La demo calcula el pronóstico, las restricciones y el plan de compra con un motor
local. Esta ruta recibe esas métricas ya calculadas y pide al modelo que las
**explique** y proponga opciones. El modelo no recalcula el pronóstico, no
sustituye el plan y no puede ordenar compras ni tocar inventario: la respuesta es
un comentario sobre cifras que ya existen.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.llm import cliente

#: Tipos de opción que la interfaz reconoce.
OPCIONES = (
    "mantener_plan",
    "adelantar_compra",
    "retrasar_compra",
    "ajustar_buffer",
    "revisar_pronostico",
    "priorizar_sku",
)

SISTEMA = (
    "Eres un analista de planificación de demanda. Recibes métricas ya calculadas "
    "por un motor determinista: parámetros del escenario, un SKU con su "
    "pronóstico y error histórico, el plan de compra propuesto, las restricciones "
    "de presupuesto y cobertura, y los indicadores de riesgo. Explica el "
    "resultado y propón opciones. Devuelve SOLO JSON con el esquema "
    '{"explicacion":"...","evidencia":["..."],"riesgos":["..."],'
    '"opciones":[{"opcion":"<tipo permitido>","efecto_esperado":"..."}]}. '
    "Reglas: no recalcules el pronóstico ni propongas cifras de compra nuevas; "
    "cita solo métricas recibidas; usa un tipo de opción de la lista permitida; "
    "entre una y tres opciones, con un efecto esperado breve; no ordenes compras, "
    "no modifiques inventario y no digas que aprobaste el plan: tu respuesta es "
    "una explicación consultiva."
)


def analizar(settings: Settings, contexto: dict[str, Any]) -> dict[str, Any]:
    """Devuelve explicación y opciones validadas sobre el plan ya calculado."""

    def validar(datos: dict[str, Any]) -> dict[str, Any]:
        opciones = datos.get("opciones")
        if not isinstance(opciones, list) or not opciones:
            raise cliente.LlmRespuestaInvalida("El modelo no devolvió opciones.")
        if len(opciones) > 3:
            raise cliente.LlmRespuestaInvalida("«opciones» admite como máximo 3 elementos.")
        limpias: list[dict[str, str]] = []
        for fila in opciones:
            if not isinstance(fila, dict):
                raise cliente.LlmRespuestaInvalida("Cada opción debe ser un objeto.")
            tipo = cliente.texto(fila.get("opcion"), campo="opcion", maximo=40)
            if tipo not in OPCIONES:
                raise cliente.LlmRespuestaInvalida(
                    "La opción no está en el catálogo del simulador."
                )
            limpias.append(
                {
                    "opcion": tipo,
                    "efecto_esperado": cliente.texto(
                        fila.get("efecto_esperado"), campo="efecto_esperado", maximo=300
                    ),
                }
            )
        return {
            "explicacion": cliente.texto(
                datos.get("explicacion"), campo="explicacion", maximo=700
            ),
            "evidencia": cliente.lista(
                datos.get("evidencia"), campo="evidencia", maximo=4, largo=220
            ),
            "riesgos": cliente.lista(
                datos.get("riesgos"), campo="riesgos", maximo=4, largo=220
            ),
            "opciones": limpias,
        }

    limpio, sobre = cliente.solicitar_json(
        settings,
        clave_limite="dockmind",
        sistema=SISTEMA,
        datos=contexto,
        validar=validar,
        max_tokens=min(int(settings.llm_max_tokens), 700),
    )
    return {
        "modo": "openai",
        "modelo": str(settings.llm_model),
        **limpio,
        "opciones_permitidas": list(OPCIONES),
        "no_recalcula_pronostico": True,
        "no_ordena_compras": True,
        "no_modifica_inventario": True,
        "advisory_only": True,
        "aviso": "simulacion",
        "uso": cliente.uso(sobre),
        "cargos_posibles": True,
    }
