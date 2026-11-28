"""Trazas OpenTelemetry del agente DIC hacia Arize Phoenix autoalojado.

La exportación está **apagada por omisión**. Solo se activa con
``PHOENIX_TRACING=true`` y un endpoint OTLP HTTP válido; no requiere cuenta de
Arize, clave de LangSmith ni servicio en la nube. Si el SDK o Phoenix no están
disponibles, el agente sigue investigando normalmente.

Se instrumenta de forma **manual** (sin auto-instrumentación de HTTP, OpenAI ni
LangChain) para controlar exactamente qué sale del proceso. Cada traza contiene:

* un span raíz ``dic.run`` con identificador, escenario, modo, orden de
  herramientas, llamadas, tokens y desenlace;
* un span ``dic.model`` por vuelta, con paso, herramientas elegidas, tokens
  reportados y si fue la respuesta final;
* un span ``dic.tool.<herramienta>`` por ejecución, con nombre, paso, duración,
  resumen acotado y ``evidencia_id``.

Nunca se exportan prompts, mensajes, argumentos de herramientas, respuestas
crudas, credenciales, la IP del visitante ni filas del dataset.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlparse

logger = logging.getLogger("app.incident_commander.observabilidad")

_ENDPOINT_POR_OMISION = "http://127.0.0.1:6006/v1/traces"
_PROYECTO_POR_OMISION = "portafolio-dic"

_CANDADO = threading.RLock()
_PROVEEDOR: Any = None
_TRACER: Any = None
_ERROR_REPORTADO = False


def _booleano(nombre: str) -> bool:
    return (os.getenv(nombre) or "").strip().lower() in {"1", "true", "yes", "on"}


def _configuracion() -> tuple[str, str] | None:
    """Endpoint y proyecto, o ``None`` si la exportación está apagada."""
    if not _booleano("PHOENIX_TRACING"):
        return None
    endpoint = (os.getenv("PHOENIX_OTLP_ENDPOINT") or _ENDPOINT_POR_OMISION).strip()
    partes = urlparse(endpoint)
    if partes.scheme not in {"http", "https"} or not partes.netloc:
        logger.warning("PHOENIX_OTLP_ENDPOINT no es una URL HTTP(S) válida; traza apagada.")
        return None
    proyecto = (os.getenv("PHOENIX_PROJECT_NAME") or _PROYECTO_POR_OMISION).strip()
    return endpoint, (proyecto or _PROYECTO_POR_OMISION)


def _proveedor() -> Any:
    """Crea una sola vez el TracerProvider; cualquier fallo lo deja apagado."""
    global _PROVEEDOR, _ERROR_REPORTADO
    with _CANDADO:
        if _PROVEEDOR is not None:
            return _PROVEEDOR
        configuracion = _configuracion()
        if configuracion is None:
            return None
        endpoint, proyecto = configuracion
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            recurso = Resource.create(
                {
                    "service.name": "portfolio-api-dic",
                    "service.version": "1",
                    "openinference.project.name": proyecto,
                }
            )
            proveedor = TracerProvider(resource=recurso)
            exportador = OTLPSpanExporter(endpoint=endpoint, timeout=2)
            proveedor.add_span_processor(
                BatchSpanProcessor(
                    exportador,
                    schedule_delay_millis=500,
                    export_timeout_millis=2000,
                    max_export_batch_size=256,
                )
            )
            _PROVEEDOR = proveedor
            _ERROR_REPORTADO = False
            return proveedor
        except Exception:
            if not _ERROR_REPORTADO:
                logger.warning(
                    "Phoenix no está disponible; la investigación continúa sin traza externa."
                )
                _ERROR_REPORTADO = True
            return None


def _tracer() -> Any:
    global _TRACER
    with _CANDADO:
        if _TRACER is not None:
            return _TRACER
        proveedor = _proveedor()
        if proveedor is None:
            return None
        _TRACER = proveedor.get_tracer("app.incident_commander", "1.0.0")
        return _TRACER


def reiniciar() -> None:
    """Olvida el proveedor actual; solo para pruebas o cambios de entorno."""
    global _PROVEEDOR, _TRACER, _ERROR_REPORTADO
    with _CANDADO:
        proveedor = _PROVEEDOR
        _PROVEEDOR = None
        _TRACER = None
        _ERROR_REPORTADO = False
    if proveedor is not None:
        try:
            proveedor.shutdown()
        except Exception:
            pass


def habilitado() -> bool:
    """True solo si la bandera está activa y el SDK pudo crear el tracer."""
    return _tracer() is not None


def estado_publico() -> dict[str, str]:
    """Estado que puede mostrarse en una respuesta; nunca incluye secretos."""
    return {"phoenix": "activado" if habilitado() else "desactivado"}


def modo_para_modelo(modelo: str) -> str:
    """Clasifica la corrida para la traza sin consultar al proveedor."""
    nombre = str(modelo or "").lower()
    return "simulado" if ("mock" in nombre or "simulad" in nombre) else "real"


def _forzar_exportacion() -> None:
    proveedor = _PROVEEDOR
    if proveedor is None:
        return
    try:
        proveedor.force_flush(timeout_millis=1500)
    except Exception:
        # La traza externa nunca condiciona la investigación.
        pass


def _cerrar(span: Any, error: BaseException | None = None, *, flush: bool = False) -> None:
    if span is None:
        return
    try:
        if error is not None:
            from opentelemetry.trace import Status, StatusCode

            span.set_attribute("dic.outcome", "error")
            span.set_attribute("dic.error", type(error).__name__)
            span.set_status(Status(StatusCode.ERROR, type(error).__name__))
        span.end()
    except Exception:
        # La traza externa nunca condiciona la investigación.
        pass
    finally:
        if flush:
            _forzar_exportacion()


@contextmanager
def traza_run(
    *, run_id: str, scenario_id: str, modo: str
) -> Iterator[Any]:
    """Span raíz de una corrida; no-op silencioso si Phoenix está apagado."""
    tracer = _tracer()
    if tracer is None:
        yield None
        return
    span = tracer.start_span("dic.run")
    span.set_attributes(
        {
            "openinference.span.kind": "AGENT",
            "dic.run_id": str(run_id),
            "dic.scenario_id": str(scenario_id),
            "dic.mode": str(modo),
            "dic.advisory_only": True,
            "dic.external_writes": 0,
        }
    )
    try:
        from opentelemetry import trace as otel_trace

        with otel_trace.use_span(span, end_on_exit=False):
            yield span
    except BaseException as exc:
        _cerrar(span, exc, flush=True)
        raise
    else:
        _cerrar(span, flush=True)


def anotar_run(span: Any, resultado: dict[str, Any]) -> None:
    """Añade el desenlace y los agregados sanitizados al span raíz."""
    if span is None:
        return
    herramientas = [str(nombre) for nombre in (resultado.get("herramientas_usadas") or [])]
    tokens = int(resultado.get("tokens_consumidos") or 0)
    span.set_attributes(
        {
            "dic.model": str(resultado.get("modelo") or ""),
            "dic.outcome": str(resultado.get("estado") or "desconocido"),
            "dic.tools_order": ",".join(herramientas),
            "dic.tools_count": len(herramientas),
            "dic.calls_model": int(resultado.get("llamadas_modelo") or 0),
            "dic.calls_tools": int(resultado.get("llamadas_herramientas") or 0),
            "dic.tokens_total": tokens,
            "llm.token_count.total": tokens,
        }
    )
    plan = (resultado.get("recomendacion") or {}).get("plan")
    if plan:
        span.set_attribute("dic.plan", str(plan))
    try:
        from opentelemetry.trace import Status, StatusCode

        span.set_status(Status(StatusCode.OK))
    except Exception:
        pass


@contextmanager
def traza_modelo(*, paso: int, modelo: str) -> Iterator[Any]:
    """Span de una llamada al modelo; no incluye mensajes ni prompts."""
    tracer = _tracer()
    if tracer is None:
        yield None
        return
    span = tracer.start_span("dic.model")
    span.set_attributes(
        {
            "openinference.span.kind": "LLM",
            "llm.model_name": str(modelo),
            "dic.step": int(paso),
        }
    )
    try:
        from opentelemetry import trace as otel_trace

        with otel_trace.use_span(span, end_on_exit=False):
            yield span
    except BaseException as exc:
        _cerrar(span, exc)
        raise
    else:
        _cerrar(span)


def anotar_modelo(
    span: Any, *, uso: dict[str, int] | None, herramientas: list[str], final: bool
) -> None:
    """Añade tokens reportados y herramientas elegidas al span del modelo."""
    if span is None:
        return
    span.set_attribute("dic.tools", ",".join(str(nombre) for nombre in herramientas))
    span.set_attribute("dic.final", bool(final))
    if isinstance(uso, dict):
        # Convención OpenInference: sin el sufijo `_tokens`.
        for clave, atributo in (
            ("prompt_tokens", "prompt"),
            ("completion_tokens", "completion"),
            ("total_tokens", "total"),
        ):
            valor = uso.get(clave)
            if isinstance(valor, int) and not isinstance(valor, bool):
                span.set_attribute(f"llm.token_count.{atributo}", valor)
    try:
        from opentelemetry.trace import Status, StatusCode

        span.set_status(Status(StatusCode.OK))
    except Exception:
        pass


@contextmanager
def traza_herramienta(*, nombre: str, paso: int) -> Iterator[Any]:
    """Span de una herramienta; no incluye argumentos ni filas crudas."""
    tracer = _tracer()
    if tracer is None:
        yield None
        return
    span = tracer.start_span(f"dic.tool.{nombre}")
    span.set_attributes(
        {
            "openinference.span.kind": "TOOL",
            "tool.name": str(nombre),
            "dic.step": int(paso),
            "dic.read_only": True,
        }
    )
    try:
        from opentelemetry import trace as otel_trace

        with otel_trace.use_span(span, end_on_exit=False):
            yield span
    except BaseException as exc:
        _cerrar(span, exc)
        raise
    else:
        _cerrar(span)


def anotar_herramienta(span: Any, resultado: dict[str, Any]) -> None:
    """Añade el resumen sanitizado y el identificador de evidencia."""
    if span is None:
        return
    span.set_attribute("dic.summary", str(resultado.get("resumen") or "")[:240])
    span.set_attribute(
        "dic.evidence_id", str(resultado.get("evidencia_id") or "")[:120]
    )
    try:
        from opentelemetry.trace import Status, StatusCode

        span.set_status(Status(StatusCode.OK))
    except Exception:
        pass
