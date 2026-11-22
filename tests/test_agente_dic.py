"""Pruebas del agente con herramientas de Incident Commander (sin red ni coste)."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routes import incident_commander as rutas
from app.config import Settings
from app.incident_commander import agente, herramientas
from app.incident_commander import service as dic_service
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
        "llm_agente_presupuesto_tokens": 1_800,
    }
    base.update(extra)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _sin_cuotas(monkeypatch):
    from app.llm import limites

    monkeypatch.setattr(
        limites, "reservar_llamada", lambda settings, *, ip=None, costo_tokens=None: {"visitante_del_dia": 1}
    )


def servicio(**extra) -> IncidentCommander:
    return IncidentCommander(ajustes(**extra))


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


def plan_final(plan: str = "quarantine") -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "recommendation": plan,
                            "justification": "La evidencia sostiene el plan.",
                            "evidence": ["Una validación falla en la copia."],
                            "risks": ["Depende del cierre de la validación."],
                        }
                    )
                }
            }
        ],
        "usage": {"total_tokens": 100},
    }


def test_catalogo_solo_lectura() -> None:
    nombres = set(herramientas.HERRAMIENTAS)
    assert nombres == {"estado_run", "validaciones", "matriz_planes", "catalogo_planes", "snapshot"}
    for prohibido in ("aprobar", "escribir_tabla", "aplicar_plan", "backfill"):
        assert prohibido not in nombres
    esquemas = herramientas.esquemas()
    assert len(esquemas) == 5
    assert all(esquema["type"] == "function" for esquema in esquemas)
    assert {esquema["function"]["name"] for esquema in esquemas} == nombres


def test_herramienta_desconocida_y_argumentos_invalidos() -> None:
    control = servicio()
    run = control.crear_run("missing")
    with pytest.raises(herramientas.HerramientaDesconocida):
        herramientas.ejecutar(control, run["run_id"], "aprobar", {})
    with pytest.raises(herramientas.ArgumentosInvalidos):
        herramientas.ejecutar(control, run["run_id"], "validaciones", "no-json")
    with pytest.raises(herramientas.ArgumentosInvalidos):
        herramientas.ejecutar(control, run["run_id"], "estado_run", {"extra": 1})
    estado = herramientas.ejecutar(control, run["run_id"], "estado_run", None)
    assert estado["run_id"] == run["run_id"]
    assert estado["aprobado"] is False


def test_agente_cierra_con_herramientas_y_plan(monkeypatch) -> None:
    control = servicio()
    run = control.crear_run("missing")
    respuestas = [tool_call("validaciones"), tool_call("catalogo_planes"), plan_final()]
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: respuestas.pop(0))
    resultado = agente.ejecutar(control, run["run_id"], control.settings)
    assert resultado["recomendacion"]["plan"] == "quarantine"
    assert resultado["pasos"] == 3
    assert resultado["solo_lectura"] is True
    assert resultado["external_writes"] == 0
    assert [entrada["tipo"] for entrada in resultado["traza"]] == [
        "herramienta",
        "herramienta",
        "final",
    ]
    assert "clave-ficticia" not in json.dumps(resultado["traza"])


def test_agente_rechaza_herramienta_fuera_de_catalogo(monkeypatch) -> None:
    control = servicio()
    run = control.crear_run("missing")
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: tool_call("escribir_tabla"))
    with pytest.raises(agente.AgentePolitica) as excinfo:
        agente.ejecutar(control, run["run_id"], control.settings)
    assert "no permitida" in str(excinfo.value)
    assert excinfo.value.traza[-1]["tipo"] == "rechazo"


def test_agente_respeta_el_limite_de_pasos(monkeypatch) -> None:
    control = servicio()
    run = control.crear_run("duplicate")
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: tool_call("estado_run"))
    with pytest.raises(agente.AgenteLimitePasos):
        agente.ejecutar(control, run["run_id"], control.settings, max_pasos=2)


def test_agente_respeta_el_presupuesto(monkeypatch) -> None:
    control = servicio()
    run = control.crear_run("schema")
    sobre = tool_call("estado_run")
    sobre["usage"] = {"total_tokens": 5_000}
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: sobre)
    with pytest.raises(agente.AgentePresupuesto):
        agente.ejecutar(control, run["run_id"], control.settings, presupuesto_tokens=1_000)


def test_agente_no_acepta_salida_invalida(monkeypatch) -> None:
    control = servicio()
    run = control.crear_run("missing")
    invalida = {"choices": [{"message": {"content": "no es json"}}], "usage": {"total_tokens": 10}}
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: invalida)
    with pytest.raises(dic_service.LlmRespuestaInvalida):
        agente.ejecutar(control, run["run_id"], control.settings)


def test_agente_no_activa_la_aprobacion_humana(monkeypatch) -> None:
    control = servicio()
    run = control.crear_run("missing")
    respuestas = [plan_final()]
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: respuestas.pop(0))
    agente.ejecutar(control, run["run_id"], control.settings)
    assert control.runs[run["run_id"]]["status"] == "awaiting_human"
    assert "approval" not in control.runs[run["run_id"]]


def test_ruta_del_agente_200_y_404(monkeypatch) -> None:
    control = servicio()
    run = control.crear_run("missing")
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: plan_final())

    app = FastAPI()
    app.include_router(rutas.router, prefix="/api/v1")
    app.dependency_overrides[rutas.get_incident_service] = lambda: control
    cliente_http = TestClient(app)

    ok = cliente_http.post(f"/api/v1/incident-commander/runs/{run['run_id']}/agent")
    assert ok.status_code == 200
    assert ok.json()["modo"] == "openai-tools"
    assert cliente_http.post("/api/v1/incident-commander/runs/DI-NO-EXISTE/agent").status_code == 404


def test_flujo_determinista_sigue_intacto() -> None:
    control = servicio()
    run = control.crear_run("missing")
    recibo = control.aprobar(run["run_id"], approved=True, plan_id="quarantine")
    assert recibo["receipt"].startswith("RCPT-")
    assert recibo["external_writes"] == 0
