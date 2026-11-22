"""Coherencia de los límites de IA al arrancar la API.

Un tope diario de tokens menor que el presupuesto de un solo run del agente deja
la ruta `/agent` inutilizable: la reserva del run nunca cabría. La API falla al
arrancar en ese caso (error explícito) en lugar de responder 429 a todo el mundo
sin explicación.
"""

from __future__ import annotations

import logging

from app.config import Settings


def problemas_de_limites(settings: Settings) -> list[str]:
    """Lista de incoherencias entre límites; vacía si la configuración es válida."""
    problemas: list[str] = []
    if int(settings.llm_max_tokens_dia) < int(settings.llm_agente_presupuesto_tokens):
        problemas.append(
            "LLM_MAX_TOKENS_DIA="
            f"{int(settings.llm_max_tokens_dia)} es menor que "
            "LLM_AGENTE_PRESUPUESTO_TOKENS="
            f"{int(settings.llm_agente_presupuesto_tokens)}: un run del agente no "
            "cabría en el presupuesto diario"
        )
    if int(settings.llm_max_tokens) > int(settings.llm_max_tokens_dia):
        problemas.append(
            "LLM_MAX_TOKENS="
            f"{int(settings.llm_max_tokens)} supera LLM_MAX_TOKENS_DIA="
            f"{int(settings.llm_max_tokens_dia)}: ni una sola consulta cabría"
        )
    return problemas


def validar_arranque(settings: Settings, logger: logging.Logger | None = None) -> None:
    """Registra el problema y falla el arranque si los límites son incoherentes."""
    problemas = problemas_de_limites(settings)
    if not problemas:
        return
    mensaje = (
        "Configuración de límites de IA incoherente: "
        + "; ".join(problemas)
        + ". Corrige deploy/.env antes de arrancar la API."
    )
    (logger or logging.getLogger("app")).critical(mensaje)
    raise RuntimeError(mensaje)
