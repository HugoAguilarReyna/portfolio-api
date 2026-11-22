"""Traducción común de los errores del proveedor a respuestas HTTP.

Las rutas de análisis comparten este envoltorio para que un fallo del modelo
nunca se convierta en un 500 opaco: sin configuración → 503, límite de frecuencia
→ 429, proveedor o contenido inválido → 502. Cada demo conserva así su respaldo
determinista cuando la consulta no está disponible.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import HTTPException, status

from app.llm import cliente


def ejecutar_consulta(accion: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Ejecuta la consulta y traduce los errores tipados del cliente."""
    try:
        return accion()
    except cliente.LlmNoConfigurado as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except cliente.LlmLimiteExcedido as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    except cliente.LlmUpstreamError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except cliente.LlmRespuestaInvalida as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
