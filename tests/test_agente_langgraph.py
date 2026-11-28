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


def test_langsmith_apagado_por_defecto(monkeypatch) -> None:
    for nombre in (
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING_V2",
        "LANGSMITH_API_KEY",
        "LANGCHAIN_API_KEY",
    ):
        monkeypatch.delenv(nombre, raising=False)
    assert observabilidad.habilitado() is False
    assert observabilidad.config_traza(run_id="DI-1", scenario_id="missing", modo="agente") == {}
    assert observabilidad.estado_publico() == {"langsmith": "desactivado"}
    with observabilidad.traza("prueba", inputs={"paso": 1}) as span:
        assert span is None


def test_langsmith_activo_solo_envia_metadatos_sanitizados(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "clave-ficticia-no-enviada")
    config = observabilidad.config_traza(
        run_id="DI-1", scenario_id="missing", modo="agente"
    )
    assert config["run_name"] == "dic-agente"
    assert config["metadata"]["run_id"] == "DI-1"
    assert config["metadata"]["advisory_only"] is True
    assert "clave-ficticia" not in json.dumps(config)
    assert "prompt" not in json.dumps(config).lower()


def test_limite_de_pasos_incluye_eventos(monkeypatch) -> None:
    control = IncidentCommander(ajustes())
    run = control.crear_run("missing")
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: tool_call("estado_run"))
    with pytest.raises(agente.AgenteLimitePasos) as excinfo:
        agente.ejecutar(control, run["run_id"], control.settings, max_pasos=2)
    assert excinfo.value.eventos
    assert excinfo.value.traza
