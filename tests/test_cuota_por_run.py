"""Cuota por run del agente con el limitador REAL (sin bypass).

El almacén es un doble en memoria, pero la lógica de reserva, la IP real, el
intervalo y los cupos son los de producción: nada se neutraliza.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from app.config import Settings
from app.db import mongo as mongo_mod
from app.incident_commander import agente
from app.incident_commander.service import IncidentCommander
from app.llm import cliente, contexto, limites

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
        return "ttl_cuotas"


class BaseFalsa:
    """Base de datos falsa: entrega la colección por nombre."""

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


class RespuestaFalsa:
    def __init__(self, sobre: dict) -> None:
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
        "llm_model": "mock-cuota-v1",
        "llm_cuota_hmac_key": CLAVE,
        "llm_max_tokens": 300,
        "llm_consultas_por_minuto": 120,
        "llm_max_solicitudes_por_ip_dia": 1,
        "llm_max_solicitudes_dia": 10,
        "llm_max_tokens_dia": 100_000,
        "llm_intervalo_minimo_s": 600,
        "llm_agente_presupuesto_tokens": 1_800,
        "llm_agente_max_pasos": 6,
    }
    base.update(extra)
    return Settings(**base)


def tool_call(nombre: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call-{nombre}",
                            "type": "function",
                            "function": {"name": nombre, "arguments": "{}"},
                        }
                    ],
                }
            }
        ],
        "usage": {"total_tokens": 100},
    }


def plan_final() -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "recommendation": "quarantine",
                            "justification": "La evidencia sostiene el plan.",
                            "evidence": ["Una validación falla."],
                            "risks": ["Depende del cierre."],
                        }
                    )
                }
            }
        ],
        "usage": {"total_tokens": 100},
    }


@pytest.fixture()
def almacen(monkeypatch):
    coleccion = ColeccionFalsa()
    monkeypatch.setattr(mongo_mod, "get_database", lambda: MongoFalsa(coleccion))
    cliente.VENTANA.reiniciar()
    return coleccion


def respuesta_http(monkeypatch, secuencia: list[dict]) -> None:
    pendientes = list(secuencia)

    def falso(peticion, timeout=None):
        return RespuestaFalsa(pendientes.pop(0))

    monkeypatch.setattr(cliente, "urlopen", falso)


def con_ip(valor: str):
    class _Ctx:
        def __enter__(self):
            self.token = contexto.fijar_ip(valor)

        def __exit__(self, *args):
            contexto.restablecer(self.token)

    return _Ctx()


def totales(almacen) -> tuple[int, int]:
    documento = next(d for clave, d in almacen.documentos.items() if clave.startswith("total:"))
    return int(documento.get("n", 0)), int(documento.get("tokens", 0))


def test_un_run_de_tres_pasos_completa_con_cupo_diario_uno(almacen, monkeypatch) -> None:
    respuesta_http(monkeypatch, [tool_call("validaciones"), tool_call("catalogo_planes"), plan_final()])
    control = IncidentCommander(ajustes())
    run = control.crear_run("missing")
    with con_ip("203.0.113.9"):
        resultado = agente.ejecutar(control, run["run_id"], control.settings)

    assert resultado["pasos"] == 3
    assert resultado["recomendacion"]["plan"] == "quarantine"
    # Una sola reserva: 1 solicitud del cupo por IP y del global, con el
    # presupuesto completo del run (1800), no 300 por paso.
    assert totales(almacen) == (1, 1_800)
    claves = [clave for clave in almacen.documentos if clave.startswith("ip:")]
    assert len(claves) == 1
    assert almacen.documentos[claves[0]]["n"] == 1


def test_el_segundo_run_de_la_misma_ip_ese_dia_recibe_429(almacen, monkeypatch) -> None:
    respuesta_http(monkeypatch, [plan_final()])
    control = IncidentCommander(ajustes())
    primero = control.crear_run("missing")
    segundo = control.crear_run("duplicate")
    with con_ip("203.0.113.9"):
        agente.ejecutar(control, primero["run_id"], control.settings)
        with pytest.raises(limites.CuotaExcedida):
            agente.ejecutar(control, segundo["run_id"], control.settings)


def test_el_cupo_global_agotado_bloquea_a_otra_ip(almacen, monkeypatch) -> None:
    respuesta_http(monkeypatch, [plan_final()])
    control = IncidentCommander(
        ajustes(llm_max_solicitudes_por_ip_dia=5, llm_max_solicitudes_dia=1, llm_intervalo_minimo_s=0)
    )
    primero = control.crear_run("missing")
    segundo = control.crear_run("duplicate")
    with con_ip("203.0.113.1"):
        agente.ejecutar(control, primero["run_id"], control.settings)
    with con_ip("198.51.100.7"):
        with pytest.raises(limites.CuotaExcedida, match="portafolio"):
            agente.ejecutar(control, segundo["run_id"], control.settings)


def test_ip_falsificada_por_cabecera_no_evade_el_limite(almacen, monkeypatch) -> None:
    respuesta_http(monkeypatch, [plan_final()])
    settings = ajustes()
    # Conexión directa (no es proxy confiable): la cabecera se ignora.
    efectiva = limites.ip_real(
        "203.0.113.9", {"x-forwarded-for": "1.2.3.4", "x-real-ip": "1.2.3.4"}, settings
    )
    assert efectiva == "203.0.113.9"

    control = IncidentCommander(settings)
    primero = control.crear_run("missing")
    segundo = control.crear_run("duplicate")
    with con_ip(efectiva):
        agente.ejecutar(control, primero["run_id"], control.settings)
    otra_efectiva = limites.ip_real(
        "203.0.113.9", {"x-forwarded-for": "5.6.7.8", "x-real-ip": "5.6.7.8"}, settings
    )
    with con_ip(otra_efectiva):
        with pytest.raises(limites.CuotaExcedida):
            agente.ejecutar(control, segundo["run_id"], control.settings)


def test_una_llamada_fuera_de_un_run_reserva_por_llamada(almacen, monkeypatch) -> None:
    respuesta_http(monkeypatch, [plan_final(), plan_final()])
    settings = ajustes(llm_max_solicitudes_dia=1, llm_intervalo_minimo_s=0)
    with con_ip("203.0.113.9"):
        cliente.post_chat(settings, {"model": "m"})
        with pytest.raises(limites.CuotaExcedida, match="portafolio|consulta de hoy"):
            cliente.post_chat(settings, {"model": "m"})


def test_un_run_que_falla_a_mitad_consume_cupo(almacen, monkeypatch) -> None:
    class Falla:
        def __enter__(self):
            raise TimeoutError("sin respuesta")

        def __exit__(self, *args):
            return False

    def falso(peticion, timeout=None):
        return Falla()

    monkeypatch.setattr(cliente, "urlopen", falso)
    control = IncidentCommander(ajustes())
    primero = control.crear_run("missing")
    segundo = control.crear_run("duplicate")
    with con_ip("203.0.113.9"):
        with pytest.raises(cliente.LlmUpstreamError):
            agente.ejecutar(control, primero["run_id"], control.settings)
        # El cupo ya se consumió: repetir exige cupo nuevo.
        with pytest.raises(limites.CuotaExcedida):
            agente.ejecutar(control, segundo["run_id"], control.settings)
