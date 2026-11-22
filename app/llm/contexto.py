"""IP del visitante por petición, solo en memoria del proceso.

El middleware de la API resuelve la IP (confiando en cabeceras únicamente si la
conexión viene de un proxy declarado) y la deja aquí. El cliente LLM la usa para
reservar cupo en el almacén compartido. Nunca se registra ni se persiste en claro.
"""

from __future__ import annotations

from contextvars import ContextVar

IP_VISITANTE: ContextVar[str | None] = ContextVar("llm_ip_visitante", default=None)


def ip_actual() -> str | None:
    return IP_VISITANTE.get()


def fijar_ip(valor: str | None):
    return IP_VISITANTE.set(valor)


def restablecer(token) -> None:
    IP_VISITANTE.reset(token)
