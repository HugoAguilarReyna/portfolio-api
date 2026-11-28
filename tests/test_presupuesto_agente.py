"""Presupuesto del run del agente: cierre ordenado y reserva coherente."""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.db import mongo as mongo_mod
from app.incident_commander import agente
from app.incident_commander.service import IncidentCommander
from app.llm import cliente, contexto, limites

CLAVE = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
PRESUPUESTO = 23000


class ColeccionFalsa:
    def __init__(self) -> None:
        self.documentos: dict[str, dict] = {}

    def find_one_and_update(self, filtro, actualizacion, upsert=False, return_document=None):
        actual = self.documentos.setdefault(filtro["_id"], {"_id": filtro["_id"]})
        for campo, valor in actualizacion.get("$inc", {}).items():
            actual[campo] = int(actual.get(campo, 0)) + int(valor)
        for campo, valor in (actualizacion.get("$set") or {}).items():
            actual[campo] = valor
        for campo, valor in actualizacion.get("$setOnInsert", {}).items():
            actual.setdefault(campo, valor)
        return dict(actual)

    def find_one(self, filtro):
        documento = self.documentos.get(filtro["_id"])
        return dict(documento) if documento else None

    def create_index(self, *args, **kwargs):
        return "ttl"


class BaseFalsa:
    def __init__(self, coleccion):
        self.coleccion = coleccion

    def __getitem__(self, nombre):
        return self.coleccion


class ClienteFalsa:
    def __init__(self, coleccion):
        self.base = BaseFalsa(coleccion)

    def __getitem__(self, nombre):
        return self.base


class DbFalsa:
    def __init__(self, coleccion):
        self.client = ClienteFalsa(coleccion)


class MongoFalsa:
    def __init__(self, coleccion):
        self.db = DbFalsa(coleccion)


class Respuesta:
    def __init__(self, sobre):
        self.sobre = sobre

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.sobre).encode("utf-8")


def ajustes(**extra) -> Settings:
    base = {
        "_env_file": None,
        "llm_base_url": "https://proveedor-simulado.invalid/v1",
        "llm_api_key": "clave-ficticia-de-prueba",
        "llm_model": "mock-presupuesto-v1",
        "llm_cuota_hmac_key": CLAVE,
        "llm_max_tokens": 300,
        "llm_consultas_por_minuto": 120,
        "llm_max_solicitudes_por_ip_dia": 10,
        "llm_max_solicitudes_dia": 10,
        "llm_max_tokens_dia": 10 ** 7,
        "llm_intervalo_minimo_s": 0,
        "llm_agente_presupuesto_tokens": PRESUPUESTO,
    }
    base.update(extra)
    return Settings(**base)


def tool_call(paso: int) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call-{paso}",
                            "type": "function",
                            "function": {"name": "validaciones", "arguments": "{}"},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 600 * paso, "completion_tokens": 40, "total_tokens": 600 * paso + 40},
    }


def plan_final(paso: int) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "recommendation": "quarantine",
                            "justification": "La evidencia sostiene el plan.",
                            "evidence": ["validaciones: Una validación falla."],
                            "risks": ["Depende del cierre."],
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 700, "completion_tokens": 60, "total_tokens": 760},
    }


@pytest.fixture()
def almacen(monkeypatch):
    coleccion = ColeccionFalsa()
    monkeypatch.setattr(mongo_mod, "get_database", lambda: MongoFalsa(coleccion))
    cliente.VENTANA.reiniciar()
    return coleccion


def servidor(monkeypatch, respuestas: list[dict]) -> None:
    pendientes = list(respuestas)

    def falso(peticion, timeout=None):
        return Respuesta(pendientes.pop(0))

    monkeypatch.setattr(cliente, "urlopen", falso)


class ConIp:
    def __enter__(self):
        self.token = contexto.fijar_ip("203.0.113.9")

    def __exit__(self, *args):
        contexto.restablecer(self.token)


def documento_total(almacen) -> dict:
    return next(d for clave, d in almacen.documentos.items() if clave.startswith("total:"))


def test_un_run_normal_de_tres_pasos_cabe_en_el_presupuesto(almacen, monkeypatch) -> None:
    servidor(monkeypatch, [tool_call(1), tool_call(2), plan_final(3)])
    control = IncidentCommander(ajustes())
    run = control.crear_run("missing")
    with ConIp():
        resultado = agente.ejecutar(control, run["run_id"], control.settings)
    assert resultado["pasos"] == 3
    assert resultado["tokens_consumidos"] == 640 + 1_240 + 760
    assert resultado["tokens_consumidos"] <= PRESUPUESTO
    assert documento_total(almacen)["tokens"] == PRESUPUESTO


def test_el_cierre_por_presupuesto_no_hace_otra_llamada_y_deja_traza(almacen, monkeypatch) -> None:
    llamadas = {"n": 0}

    def falso(peticion, timeout=None):
        llamadas["n"] += 1
        return Respuesta(tool_call(llamadas["n"]))

    monkeypatch.setattr(cliente, "urlopen", falso)
    # Presupuesto mínimo: el primer paso ya consume y el segundo no debe llamarse.
    control = IncidentCommander(ajustes(llm_agente_presupuesto_tokens=2_300))
    run = control.crear_run("duplicate")
    with ConIp():
        with pytest.raises(agente.AgentePresupuesto) as excinfo:
            agente.ejecutar(control, run["run_id"], control.settings, presupuesto_tokens=2_300)
    traza = excinfo.value.traza
    # Ninguna llamada ocurre después del corte: cada paso con herramienta tuvo una
    # llamada y la última entrada es el cierre por presupuesto.
    assert llamadas["n"] == sum(1 for entrada in traza if entrada["tipo"] == "herramienta")
    assert llamadas["n"] >= 1
    assert traza[0]["tipo"] == "herramienta"
    assert traza[-1]["tipo"] == "presupuesto"


def test_la_reserva_global_coincide_con_el_presupuesto(almacen, monkeypatch) -> None:
    servidor(monkeypatch, [plan_final(1)])
    control = IncidentCommander(ajustes())
    run = control.crear_run("schema")
    with ConIp():
        agente.ejecutar(control, run["run_id"], control.settings)
    assert documento_total(almacen)["tokens"] == PRESUPUESTO
    assert documento_total(almacen)["n"] == 1


def test_la_ruta_devuelve_la_traza_parcial_con_el_502(almacen, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.v1.routes import incident_commander as rutas

    def falso(peticion, timeout=None):
        return Respuesta(tool_call(1))

    monkeypatch.setattr(cliente, "urlopen", falso)
    control = IncidentCommander(ajustes(llm_agente_presupuesto_tokens=2_300))
    run = control.crear_run("missing")
    app = FastAPI()
    app.include_router(rutas.router, prefix="/api/v1")
    app.dependency_overrides[rutas.get_incident_service] = lambda: control
    cliente_http = TestClient(app)
    with ConIp():
        respuesta = cliente_http.post(f"/api/v1/incident-commander/runs/{run['run_id']}/agent")
    assert respuesta.status_code == 502
    cuerpo = respuesta.json()["detail"]
    assert cuerpo["motivo"] == "presupuesto"
    assert isinstance(cuerpo["traza"], list) and cuerpo["traza"]
