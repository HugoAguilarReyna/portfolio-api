"""Compensación de reservas rechazadas y validación de límites al arrancar."""

from __future__ import annotations

import inspect
import json
import threading

import pytest

from app.config import Settings
from app.db import mongo as mongo_mod
from app.incident_commander import agente
from app.incident_commander.service import IncidentCommander
from app.llm import cliente, contexto, limites
from app.llm import configuracion

CLAVE = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


class ColeccionFalsa:
    def __init__(self) -> None:
        self.documentos: dict[str, dict] = {}
        self.candado = threading.Lock()

    def find_one_and_update(self, filtro, actualizacion, upsert=False, return_document=None):
        with self.candado:
            actual = self.documentos.setdefault(filtro["_id"], {"_id": filtro["_id"]})
            for campo, valor in actualizacion.get("$inc", {}).items():
                actual[campo] = int(actual.get(campo, 0)) + int(valor)
            for campo, valor in (actualizacion.get("$set") or {}).items():
                actual[campo] = valor
            for campo, valor in actualizacion.get("$setOnInsert", {}).items():
                actual.setdefault(campo, valor)
            return dict(actual)

    def find_one(self, filtro):
        with self.candado:
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
        "llm_model": "mock-coherencia-v1",
        "llm_cuota_hmac_key": CLAVE,
        "llm_max_tokens": 300,
        "llm_consultas_por_minuto": 120,
        "llm_max_solicitudes_por_ip_dia": 10,
        "llm_max_solicitudes_dia": 10,
        "llm_max_tokens_dia": 23_000,
        "llm_intervalo_minimo_s": 0,
        "llm_agente_presupuesto_tokens": 23_000,
        "llm_agente_max_pasos": 6,
    }
    base.update(extra)
    return Settings(**base)


@pytest.fixture()
def almacen(monkeypatch):
    coleccion = ColeccionFalsa()
    monkeypatch.setattr(mongo_mod, "get_database", lambda: MongoFalsa(coleccion))
    cliente.VENTANA.reiniciar()
    return coleccion


def total(almacen) -> dict:
    return next((d for clave, d in almacen.documentos.items() if clave.startswith("total:")), {})


def ip_doc(almacen) -> dict:
    return next((d for clave, d in almacen.documentos.items() if clave.startswith("ip:")), {})


def respuesta_ok() -> dict:
    return {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def test_un_run_rechazado_no_consume_cupo_y_deja_pasar_una_consulta_menor(almacen, monkeypatch) -> None:
    """El rechazo del run (30 000 tokens) no bloquea una consulta de 300."""
    token = contexto.fijar_ip("203.0.113.9")
    try:
        control = IncidentCommander(ajustes(llm_max_tokens_dia=1_000))
        run = control.crear_run("missing")
        # El run reserva su presupuesto (23 000) y el tope diario es 1 000: se rechaza.
        with pytest.raises(limites.CuotaExcedida, match="presupuesto diario"):
            agente.ejecutar(control, run["run_id"], control.settings)
    finally:
        contexto.restablecer(token)

    # El rechazo no dejó rastro en los contadores.
    assert int(total(almacen).get("n", 0)) == 0
    assert int(total(almacen).get("tokens", 0)) == 0
    assert int(ip_doc(almacen).get("n", 0)) == 0

    # Y una consulta menor (NL-to-SQL usa el mismo transporte) sí pasa.
    monkeypatch.setattr(cliente, "urlopen", lambda peticion, timeout=None: Respuesta(respuesta_ok()))
    token = contexto.fijar_ip("203.0.113.9")
    try:
        cliente.post_chat(control.settings, {"model": "m"})  # reserva 300, cabe en 1 000
    finally:
        contexto.restablecer(token)
    assert int(total(almacen).get("n", 0)) == 1
    assert int(total(almacen).get("tokens", 0)) == 300


def test_el_rechazo_por_solicitudes_globales_tampoco_consume(almacen) -> None:
    settings = ajustes(llm_max_solicitudes_dia=1)
    limites.reservar_llamada(settings, ip="203.0.113.9", costo_tokens=300)
    assert int(total(almacen).get("n", 0)) == 1
    # La segunda choca con el cupo global: se rechaza y no deja sumada la solicitud.
    with pytest.raises(limites.CuotaExcedida, match="agotó sus consultas"):
        limites.reservar_llamada(settings, ip="198.51.100.7", costo_tokens=300)
    assert int(total(almacen).get("n", 0)) == 1


def test_la_validacion_detecta_tope_diario_menor_que_el_run() -> None:
    mala = ajustes(llm_max_tokens_dia=1_000, llm_agente_presupuesto_tokens=23_000)
    problemas = configuracion.problemas_de_limites(mala)
    assert problemas
    assert "LLM_MAX_TOKENS_DIA" in problemas[0]
    with pytest.raises(RuntimeError, match="LLM_AGENTE_PRESUPUESTO_TOKENS"):
        configuracion.validar_arranque(mala)

    assert configuracion.problemas_de_limites(ajustes()) == []
    configuracion.validar_arranque(ajustes())  # no lanza


def test_el_lifespan_llama_a_la_validacion() -> None:
    from app import main

    codigo = inspect.getsource(main.lifespan)
    assert "validar_arranque" in codigo
