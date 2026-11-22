"""IP del visitante por petición, solo en memoria del proceso.

El middleware de la API resuelve la IP (confiando en cabeceras únicamente si la
conexión viene de un proxy declarado) y la deja aquí. El cliente LLM la usa para
reservar cupo en el almacén compartido. Nunca se registra ni se persiste en claro.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

IP_VISITANTE: ContextVar[str | None] = ContextVar("llm_ip_visitante", default=None)


def ip_actual() -> str | None:
    return IP_VISITANTE.get()


def fijar_ip(valor: str | None):
    return IP_VISITANTE.set(valor)


def restablecer(token) -> None:
    IP_VISITANTE.reset(token)


#: Marca que el cupo ya se reservó para un run completo (varios pasos del agente).
RESERVA_EN_CURSO: ContextVar[bool] = ContextVar("llm_reserva_en_curso", default=False)


def reserva_activa() -> bool:
    return RESERVA_EN_CURSO.get()


@contextmanager
def reserva_de_run():
    """Indica al transporte que el cupo ya está reservado para este run.

    Se usa solo alrededor del bucle del agente, que hace **una** reserva al
    empezar; los pasos siguientes no vuelven a reservar. Fuera de este contexto,
    cada llamada reserva por sí misma.
    """
    token = RESERVA_EN_CURSO.set(True)
    try:
        yield
    finally:
        RESERVA_EN_CURSO.reset(token)
