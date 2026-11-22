"""Pruebas del Data Incident Commander sin llamadas reales al proveedor ni escrituras."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routes import incident_commander as rutas
from app.config import BACKEND_DIR, Settings
from app.incident_commander import service as servicio_mod
from app.llm import cliente
from app.incident_commander.service import (
    IncidentCommander,
    LlmNoConfigurado,
    LlmRespuestaInvalida,
    LlmUpstreamError,
    PlanBlocked,
    RunConflict,
    RunNotFound,
    ScenarioError,
)

SNAPSHOT = BACKEND_DIR / "data" / "snapshots" / "incident-commander" / "data.json"

#: Configuración ficticia: estas pruebas nunca abren una conexión.
LLM_FICTICIO = {
    "llm_base_url": "https://proveedor.invalid/v1",
    "llm_api_key": "clave-de-prueba-no-real",
    "llm_model": "modelo-de-prueba",
}


def servicio() -> IncidentCommander:
    return IncidentCommander(
        Settings(
            _env_file=None,
            incident_data_path=str(SNAPSHOT),
            # Sin proveedor: la consulta queda deshabilitada de forma determinista
            # aunque el entorno tenga variables LLM_* exportadas.
            llm_base_url=None,
            llm_api_key=None,
            llm_model=None,
        )
    )


def servicio_con_llm() -> IncidentCommander:
    return IncidentCommander(
        Settings(
            _env_file=None,
            incident_data_path=str(SNAPSHOT),
            llm_consultas_por_minuto=120,
            **LLM_FICTICIO,
        )
    )

@pytest.fixture(autouse=True)
def _ventana_limpia():
    """Cada prueba arranca con la ventana de consultas vacía."""
    cliente.VENTANA.reiniciar()
    yield
    cliente.VENTANA.reiniciar()

@pytest.fixture(autouse=True)
def _sin_cuotas_compartidas(monkeypatch):
    """Las cuotas compartidas tienen su propio archivo; aquí se anulan."""
    from app.llm import limites

    monkeypatch.setattr(
        limites, "reservar_llamada", lambda settings, *, ip=None: {"visitante_del_dia": 1}
    )


def sobre(**contenido) -> dict:
    """Sobre de Chat Completions con el contenido indicado."""
    datos = {
        "recommendation": "quarantine",
        "justification": "La cuarentena aísla el defecto y conserva la traza completa.",
        "evidence": ["Cobertura 2019-2025 incompleta en la copia."],
        "risks": ["Requiere recargar la fuente oficial."],
    }
    datos.update(contenido)
    return {
        "choices": [{"message": {"content": json.dumps(datos, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
    }


def cliente_de(servicio_activo: IncidentCommander) -> TestClient:
    app = FastAPI()
    app.include_router(rutas.router, prefix="/api/v1")
    app.dependency_overrides[rutas.get_incident_service] = lambda: servicio_activo
    return TestClient(app)


def test_snapshot_real_de_21_observaciones() -> None:
    app = servicio()
    salud = app.salud()
    assert salud["observations"] == 21
    assert salud["llm"] is False
    assert salud["atlas"] is False
    assert salud["external_writes"] == 0
    assert salud["source_digest"] == app.source_digest
    assert len({(r["year"], r["indicator_code"]) for r in app.clean}) == 21


def test_tres_escenarios_inyectan_falla_solo_en_copia() -> None:
    app = servicio()
    escenarios = {
        "missing": (20, "period_coverage"),
        "duplicate": (24, "key_uniqueness"),
        "schema": (21, "catalog_codes"),
    }
    for scenario, (count, failed_check) in escenarios.items():
        resultado = app.crear_run(scenario)
        assert resultado["observations_in_copy"] == count
        assert resultado["observations_in_source"] == 21
        assert resultado["status"] == "awaiting_human"
        assert resultado["external_writes"] == 0
        baseline = {check["id"]: check["passed"] for check in resultado["baseline_checks"]}
        assert baseline[failed_check] is False
        assert all(
            check["passed"]
            for check in resultado["candidate_checks"]
            if check["id"] != "human_approval_recorded"
        )
        assert resultado["recommended_plan"] == "quarantine"
        assert app.source_digest == resultado["source_digest"]


def test_planes_permitidos_y_aprobacion_local() -> None:
    app = servicio()
    for plan in ("quarantine", "backfill", "fullreload"):
        run = app.crear_run("missing")
        respuesta = app.aprobar(run["run_id"], approved=True, plan_id=plan)
        assert respuesta["status"] == "sandbox_simulation_completed"
        assert respuesta["receipt"].startswith("RCPT-")
        assert respuesta["external_writes"] == 0
        assert respuesta["writes"] == []
        assert respuesta["source_unchanged"] is True
        assert all(check["passed"] for check in respuesta["tests_after"])


def test_imputacion_bloqueada_y_rechazo_sin_efecto() -> None:
    app = servicio()
    run = app.crear_run("missing")
    with pytest.raises(PlanBlocked, match="imputación"):
        app.aprobar(run["run_id"], approved=True, plan_id="forward")
    assert app.runs[run["run_id"]]["status"] == "awaiting_human"

    rechazo = app.aprobar(run["run_id"], approved=False, plan_id="forward")
    assert rechazo["status"] == "rejected_no_effect"
    assert rechazo["receipt"] is None
    assert rechazo["external_writes"] == 0
    assert rechazo["source_unchanged"] is True


def test_rechazo_normal_y_doble_aprobacion() -> None:
    app = servicio()
    run = app.crear_run("duplicate")
    rechazo = app.aprobar(run["run_id"], approved=False, plan_id="quarantine")
    assert rechazo["status"] == "rejected_no_effect"
    with pytest.raises(RunConflict):
        app.aprobar(run["run_id"], approved=True, plan_id="quarantine")

    run2 = app.crear_run("schema")
    app.aprobar(run2["run_id"], approved=True, plan_id="quarantine")
    with pytest.raises(RunConflict):
        app.aprobar(run2["run_id"], approved=True, plan_id="quarantine")


def test_run_inexistente_y_escenario_invalido() -> None:
    app = servicio()
    with pytest.raises(RunNotFound):
        app.aprobar("DI-NO-EXISTE", approved=False, plan_id="quarantine")
    with pytest.raises(ScenarioError):
        app.crear_run("otro")


def test_estado_es_efimero_por_proceso() -> None:
    primero = servicio()
    run = primero.crear_run("missing")
    assert run["run_id"] in primero.runs
    segundo = servicio()
    with pytest.raises(RunNotFound):
        segundo.aprobar(run["run_id"], approved=False, plan_id="quarantine")


def test_fuente_sigue_intacta_y_no_hay_escrituras(tmp_path) -> None:
    copia = tmp_path / "data.json"
    copia.write_bytes(SNAPSHOT.read_bytes())
    app = IncidentCommander(
        Settings(_env_file=None, incident_data_path=str(copia))
    )
    digest_antes = hashlib.sha256(copia.read_bytes()).hexdigest()
    run = app.crear_run("schema")
    app.aprobar(run["run_id"], approved=True, plan_id="quarantine")
    digest_despues = hashlib.sha256(copia.read_bytes()).hexdigest()
    assert digest_antes == digest_despues
    assert app.salud()["external_writes"] == 0
    assert list(Path(tmp_path).glob("*")) == [copia]


# --------------------------------------------------------- consulta al proveedor


def test_payload_va_a_chat_completions_con_la_credencial_en_cabecera(monkeypatch) -> None:
    """El endpoint es Chat Completions y la clave nunca viaja en el cuerpo."""
    capturado = {}

    class Respuesta:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(sobre()).encode("utf-8")

    def falso_urlopen(request, timeout=None):
        capturado["url"] = request.full_url
        capturado["headers"] = dict(request.header_items())
        capturado["body"] = request.data.decode("utf-8")
        capturado["timeout"] = timeout
        return Respuesta()

    monkeypatch.setattr(cliente, "urlopen", falso_urlopen)
    settings = Settings(_env_file=None, **LLM_FICTICIO)
    resultado = servicio_mod._post_chat(settings, {"model": "modelo-de-prueba", "messages": []})

    assert resultado["choices"][0]["message"]["content"]
    assert capturado["url"] == "https://proveedor.invalid/v1/chat/completions"
    assert capturado["headers"]["Authorization"] == f"Bearer {LLM_FICTICIO['llm_api_key']}"
    assert LLM_FICTICIO["llm_api_key"] not in capturado["body"]


def test_sin_configuracion_la_consulta_no_esta_disponible() -> None:
    app = servicio()
    run = app.crear_run("missing")
    assert app.llm_configurado is False
    assert app.salud()["llm"] is False
    with pytest.raises(LlmNoConfigurado):
        app.analizar(run["run_id"])


def test_consulta_valida_es_consultiva_y_no_cambia_el_run(monkeypatch) -> None:
    app = servicio_con_llm()
    run = app.crear_run("missing")
    capturado = {}

    def falso_post(settings, payload, *, timeout=30.0):
        capturado["payload"] = payload
        return sobre()

    monkeypatch.setattr(servicio_mod, "_post_chat", falso_post)
    respuesta = app.analizar(run["run_id"])

    assert respuesta["recommendation"]["plan"] == "quarantine"
    assert respuesta["recommendation"]["allowed"] is True
    assert respuesta["recommendation"]["forward_blocked"] is True
    assert respuesta["advisory_only"] is True
    assert respuesta["approved"] is False
    assert respuesta["executed"] is False
    assert respuesta["approval_required"] is True
    assert respuesta["external_writes"] == 0
    assert respuesta["justification"].startswith("La cuarentena")
    assert respuesta["evidence"] and respuesta["risks"]
    assert respuesta["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 22,
        "total_tokens": 33,
    }

    # El run sigue esperando la decisión humana, sin aprobación registrada.
    assert app.runs[run["run_id"]]["status"] == "awaiting_human"
    assert "approval" not in app.runs[run["run_id"]]

    # El envío incluye el catálogo y no contiene la credencial.
    payload = capturado["payload"]
    assert LLM_FICTICIO["llm_api_key"] not in json.dumps(payload)
    assert payload["model"] == LLM_FICTICIO["llm_model"]
    assert payload["response_format"] == {"type": "json_object"}
    contexto = json.loads(payload["messages"][1]["content"])
    assert contexto["plans_allowed"] == ["quarantine", "backfill", "fullreload"]
    assert contexto["plans_blocked"] == ["forward"]
    assert contexto["write_authority"] == "none"


def test_forward_y_planes_fuera_del_catalogo_se_rechazan(monkeypatch) -> None:
    app = servicio_con_llm()
    run = app.crear_run("missing")
    for contenido, esperado in (
        ({"recommendation": "forward"}, "bloquea"),
        ({"recommendation": "restore"}, "permitidos"),
        ({"recommendation": "quarantine+backfill"}, "permitidos"),
    ):
        monkeypatch.setattr(servicio_mod, "_post_chat", lambda *a, _c=contenido, **k: sobre(**_c))
        with pytest.raises(LlmRespuestaInvalida, match=esperado):
            app.analizar(run["run_id"])
    assert "analysis" not in app.runs[run["run_id"]]
    assert app.runs[run["run_id"]]["status"] == "awaiting_human"

    # El plan se normaliza a minúsculas: «FULLRELOAD» es el plan permitido.
    normalizado = servicio_mod.validar_recomendacion(
        json.dumps(
            {
                "recommendation": " FULLRELOAD ",
                "justification": "Recarga integral en ventana acotada.",
                "evidence": ["Copia íntegra disponible."],
                "risks": ["Mayor costo de cómputo."],
            }
        )
    )
    assert normalizado["plan"] == "fullreload"


def test_respuestas_invalidas_del_modelo(monkeypatch) -> None:
    app = servicio_con_llm()
    casos = [
        "no es json",
        json.dumps({"recommendation": "quarantine"}),
        json.dumps({"recommendation": "quarantine", "justification": "   ", "evidence": ["a"], "risks": ["b"]}),
        json.dumps({"recommendation": "quarantine", "justification": "ok", "evidence": [], "risks": ["b"]}),
        json.dumps({"recommendation": "quarantine", "justification": "ok", "evidence": ["a"], "risks": "no"}),
        json.dumps({"recommendation": "quarantine", "justification": "ok", "evidence": ["x"] * 7, "risks": ["b"]}),
    ]
    for contenido in casos:

        def falso_post(settings, payload, *, timeout=30.0, _c=contenido):
            return {"choices": [{"message": {"content": _c}}]}

        monkeypatch.setattr(servicio_mod, "_post_chat", falso_post)
        run = app.crear_run("missing")
        with pytest.raises(LlmRespuestaInvalida):
            app.analizar(run["run_id"])


def test_sobre_sin_mensaje_utilizable_es_error_de_proveedor(monkeypatch) -> None:
    app = servicio_con_llm()
    run = app.crear_run("missing")
    for malo in ({}, {"choices": []}, {"choices": [{"message": {"content": ""}}]}):
        monkeypatch.setattr(servicio_mod, "_post_chat", lambda *a, _m=malo, **k: _m)
        with pytest.raises(LlmUpstreamError):
            app.analizar(run["run_id"])


def test_la_aprobacion_humana_sigue_siendo_la_unica_via(monkeypatch) -> None:
    app = servicio_con_llm()
    run = app.crear_run("duplicate")
    monkeypatch.setattr(
        servicio_mod, "_post_chat", lambda *a, **k: sobre(recommendation="backfill")
    )
    consulta = app.analizar(run["run_id"])
    assert consulta["recommendation"]["plan"] == "backfill"
    assert app.runs[run["run_id"]]["status"] == "awaiting_human"

    # La persona aprueba otro plan: la consulta no se ejecutó ni se aplicó.
    recibo = app.aprobar(run["run_id"], approved=True, plan_id="quarantine")
    assert recibo["plan_id"] == "quarantine"
    assert recibo["receipt"].startswith("RCPT-")
    assert recibo["external_writes"] == 0

    otra = app.analizar(run["run_id"])
    assert otra["human_approval"] == "recorded"
    assert otra["approved"] is False and otra["executed"] is False
    assert app.runs[run["run_id"]]["receipt"] == recibo["receipt"]


def test_salud_reporta_el_modelo_sin_filtrar_la_credencial() -> None:
    app = servicio_con_llm()
    salud = app.salud()
    assert salud["llm"] is True
    assert salud["llm_model"] == LLM_FICTICIO["llm_model"]
    assert salud["llm_executes"] is False
    assert LLM_FICTICIO["llm_api_key"] not in json.dumps(salud)


def test_ruta_analysis_ok_404_503_y_502(monkeypatch) -> None:
    app = servicio_con_llm()
    run = app.crear_run("missing")
    cliente = cliente_de(app)
    monkeypatch.setattr(servicio_mod, "_post_chat", lambda *a, **k: sobre())

    ok = cliente.post(f"/api/v1/incident-commander/runs/{run['run_id']}/analysis")
    assert ok.status_code == 200
    cuerpo = ok.json()
    assert cuerpo["advisory_only"] is True
    assert cuerpo["recommendation"]["plan"] == "quarantine"
    assert cuerpo["run_status"] == "awaiting_human"

    inexistente = cliente.post("/api/v1/incident-commander/runs/DI-NO-EXISTE/analysis")
    assert inexistente.status_code == 404

    otro = servicio()
    run_sin = otro.crear_run("missing")
    sin_llm = cliente_de(otro).post(
        f"/api/v1/incident-commander/runs/{run_sin['run_id']}/analysis"
    )
    assert sin_llm.status_code == 503

    monkeypatch.setattr(
        servicio_mod, "_post_chat", lambda *a, **k: sobre(recommendation="forward")
    )
    bloqueado = cliente.post(f"/api/v1/incident-commander/runs/{run['run_id']}/analysis")
    assert bloqueado.status_code == 502
    assert LLM_FICTICIO["llm_api_key"] not in bloqueado.text

