"""Pruebas del presupuesto monetario aplicado en el transporte compartido."""

from __future__ import annotations

import json
from urllib.error import URLError

import pytest

from app.config import Settings
from app.llm import cliente, contexto, presupuesto

TARIFA_PRUEBA = {"gpt-4.1-mini": (0.40, 1.60)}


class _Respuesta:
    def __init__(self, sobre: dict) -> None:
        self._cuerpo = json.dumps(sobre).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._cuerpo


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        llm_base_url="https://proveedor-simulado.invalid/v1",
        llm_api_key="clave-ficticia-de-prueba",
        llm_model="gpt-4.1-mini",
        llm_max_tokens=300,
        llm_consultas_por_minuto=120,
    )


def _payload(*, max_tokens: int = 100, tools: bool = False) -> dict:
    datos = {
        "model": "gpt-4.1-mini",
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": "Incidente sintético de prueba."}],
    }
    if tools:
        datos["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "consultar_fuente",
                    "description": "Consulta una fuente de evidencia.",
                    "parameters": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "required": ["id"],
                    },
                },
            }
        ]
    return datos


def test_cota_incluye_esquemas_de_herramientas_y_maximo_de_salida() -> None:
    gestor = presupuesto.PresupuestoMonetario(1.0, TARIFA_PRUEBA)
    simple = gestor.cota_usd(_payload(max_tokens=100))
    con_herramientas = gestor.cota_usd(_payload(max_tokens=100, tools=True))
    mas_salida = gestor.cota_usd(_payload(max_tokens=200))
    assert con_herramientas > simple
    assert mas_salida > simple


def test_uso_reportado_concilia_y_libera_la_reserva(monkeypatch) -> None:
    sobre = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }
    monkeypatch.setattr(cliente, "urlopen", lambda *args, **kwargs: _Respuesta(sobre))
    gestor = presupuesto.PresupuestoMonetario(0.01, TARIFA_PRUEBA)
    with presupuesto.usar(gestor), contexto.reserva_de_run():
        respuesta = cliente.post_chat(_settings(), _payload())

    assert respuesta == sobre
    esperado = (100 * 0.40 + 20 * 1.60) / 1e6
    assert gestor.gastado_usd == pytest.approx(esperado)
    assert gestor.reservado_usd == 0
    assert gestor.disponible_usd == pytest.approx(0.01 - esperado)
    resumen = gestor.resumen()
    assert resumen["conciliadas"] == 1
    assert resumen["llamadas_facturables"] == 1
    assert resumen["sin_uso_reportado"] == 0


def test_varias_llamadas_se_detienen_antes_de_exceder_el_saldo(monkeypatch) -> None:
    """Cada solicitud sin `usage` conserva su cota; la tercera ya no cabe."""
    sobre = {"choices": [{"message": {"content": "ok"}}]}
    llamadas = {"n": 0}

    def urlopen(*args, **kwargs):
        llamadas["n"] += 1
        return _Respuesta(sobre)

    monkeypatch.setattr(cliente, "urlopen", urlopen)
    cota = presupuesto.PresupuestoMonetario(1.0, TARIFA_PRUEBA).cota_usd(_payload())
    gestor = presupuesto.PresupuestoMonetario(cota * 2.5, TARIFA_PRUEBA)

    with presupuesto.usar(gestor), contexto.reserva_de_run():
        cliente.post_chat(_settings(), _payload())
        cliente.post_chat(_settings(), _payload())
        with pytest.raises(presupuesto.PresupuestoAgotado):
            cliente.post_chat(_settings(), _payload())

    assert llamadas["n"] == 2
    assert gestor.reservado_usd == 0
    assert gestor.gastado_usd == pytest.approx(cota * 2)
    assert gestor.resumen()["sin_uso_reportado"] == 2


def test_tarifa_desconocida_no_hace_ninguna_solicitud(monkeypatch) -> None:
    llamadas = {"n": 0}

    def urlopen(*args, **kwargs):
        llamadas["n"] += 1
        return _Respuesta({"choices": [{"message": {"content": "no"}}]})

    monkeypatch.setattr(cliente, "urlopen", urlopen)
    gestor = presupuesto.PresupuestoMonetario(1.0, TARIFA_PRUEBA)
    payload = _payload()
    payload["model"] = "modelo-sin-tarifa"

    with presupuesto.usar(gestor), contexto.reserva_de_run():
        with pytest.raises(presupuesto.TarifaDesconocida):
            cliente.post_chat(_settings(), payload)

    assert llamadas["n"] == 0
    assert gestor.resumen()["llamadas_facturables"] == 0


def test_fallo_ambiguo_conserva_la_reserva(monkeypatch) -> None:
    llamadas = {"n": 0}

    def urlopen(*args, **kwargs):
        llamadas["n"] += 1
        raise URLError("timeout simulado")

    monkeypatch.setattr(cliente, "urlopen", urlopen)
    gestor = presupuesto.PresupuestoMonetario(0.01, TARIFA_PRUEBA)
    cota = gestor.cota_usd(_payload())
    gestor = presupuesto.PresupuestoMonetario(cota * 1.2, TARIFA_PRUEBA)

    with presupuesto.usar(gestor), contexto.reserva_de_run():
        with pytest.raises(cliente.LlmUpstreamError):
            cliente.post_chat(_settings(), _payload())
        assert gestor.gastado_usd == pytest.approx(cota)
        assert gestor.reservado_usd == 0
        with pytest.raises(presupuesto.PresupuestoAgotado):
            cliente.post_chat(_settings(), _payload())

    assert llamadas["n"] == 1
    resumen = gestor.resumen()
    assert resumen["fallidas"] == 1
    assert resumen["sin_uso_reportado"] == 1
    assert resumen["disponible_usd"] < cota
