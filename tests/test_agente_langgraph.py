"""Pruebas del grafo LangGraph: rutas distintas, evidencia y paradas."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routes import incident_commander as rutas
from app.config import Settings
from app.incident_commander import agente, grafo, observabilidad
from app.incident_commander.service import IncidentCommander
from app.llm import cliente

CLAVE = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def ajustes(**extra) -> Settings:
    base = {
        "_env_file": None,
        "llm_base_url": "https://proveedor-simulado.invalid/v1",
        "llm_api_key": "clave-ficticia-de-evaluacion",
        "llm_model": "mock-agente-v1",
        "llm_cuota_hmac_key": CLAVE,
        "llm_consultas_por_minuto": 120,
        "llm_agente_max_pasos": 6,
        "llm_agente_max_segundos": 60,
        "llm_agente_presupuesto_tokens": 23_000,
    }
    base.update(extra)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _sin_cuotas(monkeypatch):
    from app.llm import limites

    monkeypatch.setattr(
        limites,
        "reservar_llamada",
        lambda settings, *, ip=None, costo_tokens=None: {"visitante_del_dia": 1},
    )
    monkeypatch.setattr(
        limites,
        "reservar_run",
        lambda settings, *, ip=None, presupuesto_tokens=None: {"visitante_del_dia": 1},
    )


@pytest.fixture(autouse=True)
def _phoenix_limpio(monkeypatch):
    for nombre in ("PHOENIX_TRACING", "PHOENIX_OTLP_ENDPOINT", "PHOENIX_PROJECT_NAME"):
        monkeypatch.delenv(nombre, raising=False)
    observabilidad.reiniciar()
    yield
    observabilidad.reiniciar()


def tool_call(nombre: str, argumentos: dict | None = None, *, identificador: str = "call-1") -> dict:
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
                            "function": {"name": nombre, "arguments": json.dumps(argumentos or {})},
                        }
                    ],
                }
            }
        ],
        "usage": {"total_tokens": 100},
    }


def plan_final(plan: str = "quarantine", evidencia: list[str] | None = None) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "recommendation": plan,
                            "justification": "La evidencia sostiene el plan.",
                            "evidence": evidencia or ["validaciones: Una validación falla en la copia."],
                            "risks": ["Depende del cierre de la validación."],
                        }
                    )
                }
            }
        ],
        "usage": {"total_tokens": 100},
    }


def test_rutas_distintas_por_incidente(monkeypatch) -> None:
    control = IncidentCommander(ajustes())
    run_a = control.crear_run("missing")
    respuestas_a = [
        tool_call("perfil_copia"),
        tool_call("linaje"),
        plan_final(evidencia=["perfil_copia: Faltan periodos PIB.", "linaje: Activos aguas abajo."]),
    ]
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: respuestas_a.pop(0))
    resultado_a = agente.ejecutar(control, run_a["run_id"], control.settings)
    assert resultado_a["herramientas_usadas"] == ["perfil_copia", "linaje"]
    assert resultado_a["modo"] == "langgraph-openai-tools"

    run_b = control.crear_run("duplicate")
    respuestas_b = [
        tool_call("validaciones"),
        plan_final(evidencia=["validaciones: La unicidad falla."]),
    ]
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: respuestas_b.pop(0))
    resultado_b = agente.ejecutar(control, run_b["run_id"], control.settings)
    assert resultado_b["herramientas_usadas"] == ["validaciones"]
    assert resultado_a["herramientas_usadas"] != resultado_b["herramientas_usadas"]


def test_eventos_observables_sin_prompt_ni_razonamiento(monkeypatch) -> None:
    control = IncidentCommander(ajustes())
    run = control.crear_run("missing")
    respuestas = [tool_call("perfil_copia"), plan_final(evidencia=["perfil_copia: Faltan periodos."])]
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: respuestas.pop(0))
    resultado = agente.ejecutar(control, run["run_id"], control.settings)
    tipos = [evento["tipo"] for evento in resultado["eventos"]]
    assert "entrada" in tipos
    assert "seleccion" in tipos
    assert "herramienta" in tipos
    assert "resultado" in tipos
    assert "validacion" in tipos
    assert "recomendacion" in tipos
    assert "revision_humana" in tipos
    serializado = json.dumps(resultado["eventos"], ensure_ascii=False).lower()
    assert "eres un agente" not in serializado
    assert "clave-ficticia" not in serializado


def test_determinista_sin_llm_ni_cuota(monkeypatch) -> None:
    control = IncidentCommander(
        ajustes(llm_base_url=None, llm_api_key=None, llm_model=None)
    )
    run = control.crear_run("missing")
    llamado = {"n": 0}

    def no_llamar(*args, **kwargs):
        llamado["n"] += 1
        raise AssertionError("el flujo determinista no debe llamar al modelo")

    monkeypatch.setattr(cliente, "post_chat", no_llamar)
    resultado = agente.ejecutar_determinista(control, run["run_id"])
    assert resultado["modo"] == "determinista"
    assert llamado["n"] == 0
    assert resultado["tokens_consumidos"] == 0
    assert resultado["presupuesto_tokens"] == 0
    assert resultado["recomendacion"]["plan"] == "quarantine"
    assert resultado["llamadas_herramientas"] == 3
    assert all(":" in item for item in resultado["evidencia"])


def test_evidencia_no_vinculada_se_rechaza(monkeypatch) -> None:
    control = IncidentCommander(ajustes())
    run = control.crear_run("missing")
    respuestas = [
        tool_call("estado_run"),
        plan_final(evidencia=["inventada: algo"]),
        plan_final(evidencia=["inventada: algo"]),
    ]
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: respuestas.pop(0))
    with pytest.raises(cliente.LlmRespuestaInvalida):
        agente.ejecutar(control, run["run_id"], control.settings)


def test_sin_herramientas_se_detiene_por_evidencia_insuficiente(monkeypatch) -> None:
    control = IncidentCommander(ajustes())
    run = control.crear_run("missing")
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: plan_final())
    resultado = agente.ejecutar(control, run["run_id"], control.settings)
    assert resultado["estado"] == "detenido"
    assert resultado["motivo_parada"] == "evidencia_insuficiente"
    assert resultado["recomendacion"] is None
    assert resultado["eventos"][-1]["tipo"] == "parada"


def test_tiempo_de_pared(monkeypatch) -> None:
    control = IncidentCommander(ajustes())
    run = control.crear_run("missing")
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: tool_call("estado_run"))
    with pytest.raises(agente.AgenteTiempo):
        agente.ejecutar(control, run["run_id"], control.settings, max_segundos=0.000001)


def test_ruta_determinista_sin_configuracion() -> None:
    control = IncidentCommander(
        ajustes(llm_base_url=None, llm_api_key=None, llm_model=None)
    )
    run = control.crear_run("missing")
    app = FastAPI()
    app.include_router(rutas.router, prefix="/api/v1")
    app.dependency_overrides[rutas.get_incident_service] = lambda: control
    http = TestClient(app)
    respuesta = http.post(
        f"/api/v1/incident-commander/runs/{run['run_id']}/agent?flujo=determinista"
    )
    assert respuesta.status_code == 200
    assert respuesta.json()["modo"] == "determinista"


def test_phoenix_apagado_por_defecto() -> None:
    assert observabilidad.habilitado() is False
    assert observabilidad.estado_publico() == {"phoenix": "desactivado"}
    with observabilidad.traza_run(run_id="DI-1", scenario_id="missing", modo="simulado") as span:
        assert span is None
    with observabilidad.traza_modelo(paso=1, modelo="mock-agente-v1") as span:
        assert span is None
    with observabilidad.traza_herramienta(nombre="perfil_copia", paso=1) as span:
        assert span is None


def test_phoenix_exporta_spans_sanitizados_de_una_corrida(monkeypatch) -> None:
    """Captura los spans en memoria: no hay red, pero sí SDK real de OTel."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exportador = InMemorySpanExporter()
    proveedor = TracerProvider()
    proveedor.add_span_processor(SimpleSpanProcessor(exportador))
    monkeypatch.setattr(observabilidad, "_PROVEEDOR", proveedor)
    monkeypatch.setattr(observabilidad, "_TRACER", None)

    control = IncidentCommander(ajustes())
    run = control.crear_run("missing")
    respuestas = [
        tool_call("perfil_copia"),
        tool_call("linaje"),
        plan_final(
            evidencia=[
                "perfil_copia: Faltan periodos PIB.",
                "linaje: Activos aguas abajo.",
            ]
        ),
    ]
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: respuestas.pop(0))
    resultado = agente.ejecutar(control, run["run_id"], control.settings)

    spans = exportador.get_finished_spans()
    por_nombre = {span.name: span for span in spans}
    raiz = por_nombre["dic.run"]
    assert raiz.attributes["dic.run_id"] == run["run_id"]
    assert raiz.attributes["dic.mode"] == "simulado"
    assert raiz.attributes["dic.tools_order"] == "perfil_copia,linaje"
    assert raiz.attributes["dic.outcome"] == "completado"
    assert raiz.attributes["dic.calls_model"] == 3
    assert raiz.attributes["dic.calls_tools"] == 2
    assert raiz.attributes["dic.tokens_total"] == 300
    assert raiz.attributes["dic.external_writes"] == 0

    modelos = [span for span in spans if span.name == "dic.model"]
    herramientas = [span for span in spans if span.name.startswith("dic.tool.")]
    assert len(modelos) == 3
    assert {span.name for span in herramientas} == {
        "dic.tool.perfil_copia",
        "dic.tool.linaje",
    }
    assert all(span.parent.span_id == raiz.context.span_id for span in modelos + herramientas)
    assert modelos[-1].attributes["dic.final"] is True
    assert modelos[0].attributes["llm.token_count.total"] == 100
    assert modelos[0].attributes["dic.tools"] == "perfil_copia"

    serializado = json.dumps(
        [
            {"nombre": span.name, "atributos": dict(span.attributes)}
            for span in spans
        ],
        ensure_ascii=False,
        default=str,
    ).lower()
    for prohibido in (
        "eres un agente",
        "clave-ficticia",
        "prompt",
        "argumentos",
        "respuesta cruda",
        "perfil_copia: faltan periodos",
        "linaje: activos aguas abajo",
        "127.0.0.1",
    ):
        assert prohibido not in serializado
    assert resultado["observabilidad"] == {"phoenix": "activado"}


def test_phoenix_endpoint_invalido_no_interrumpe(monkeypatch) -> None:
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_OTLP_ENDPOINT", "file:///tmp/traza")
    observabilidad.reiniciar()
    assert observabilidad.habilitado() is False

    control = IncidentCommander(ajustes())
    run = control.crear_run("missing")
    respuestas = [
        tool_call("perfil_copia"),
        plan_final(evidencia=["perfil_copia: Faltan periodos."]),
    ]
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: respuestas.pop(0))
    resultado = agente.ejecutar(control, run["run_id"], control.settings)
    assert resultado["estado"] == "completado"


def test_limite_de_pasos_incluye_eventos(monkeypatch) -> None:
    control = IncidentCommander(ajustes())
    run = control.crear_run("missing")
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: tool_call("estado_run"))
    with pytest.raises(agente.AgenteLimitePasos) as excinfo:
        agente.ejecutar(control, run["run_id"], control.settings, max_pasos=2)
    assert excinfo.value.eventos
    assert excinfo.value.traza
