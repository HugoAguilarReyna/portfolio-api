"""Pruebas de las cuotas compartidas, del tope de cuerpo y del guardián de rutas.

Sin red y sin MongoDB real: se usa un doble de la colección que imita las
operaciones atómicas que usa el limitador.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

from app.api.deps import exigir_cuota_llm
from app.config import Settings, get_settings
from app.llm import cliente, limites
from app.main import LimiteCuerpoMiddleware, create_app


class ColeccionFalsa:
    """Imita `find_one_and_update`, `find_one` y `create_index`."""

    def __init__(self, falla: bool = False) -> None:
        self.documentos: dict[str, dict] = {}
        self.falla = falla

    def find_one_and_update(self, filtro, actualizacion, upsert=False, return_document=None):
        if self.falla:
            raise PyMongoError("sin almacén")
        clave = filtro["_id"]
        actual = self.documentos.setdefault(clave, {"_id": clave})
        for campo, valor in actualizacion.get("$inc", {}).items():
            actual[campo] = int(actual.get(campo, 0)) + int(valor)
        for campo, valor in actualizacion.get("$setOnInsert", {}).items():
            actual.setdefault(campo, valor)
        return actual

    def find_one(self, filtro):
        if self.falla:
            raise PyMongoError("sin almacén")
        return self.documentos.get(filtro["_id"])

    def create_index(self, *args, **kwargs):
        if self.falla:
            raise PyMongoError("sin almacén")
        return "ttl_cuotas"


class BaseFalsa(dict):
    """Expone la colección por nombre, como `Database.__getitem__`."""

    def __init__(self, coleccion: ColeccionFalsa) -> None:
        super().__init__()
        self.coleccion = coleccion

    def __getitem__(self, nombre):
        return self.coleccion


def ajustes(**extra) -> Settings:
    base = {
        "_env_file": None,
        "llm_base_url": "https://proveedor.invalid/v1",
        "llm_api_key": "clave-ficticia-de-prueba",
        "llm_model": "modelo-de-prueba",
        "llm_consultas_por_minuto": 2,
        "llm_max_solicitudes_por_ip_dia": 3,
        "llm_max_solicitudes_dia": 5,
        "llm_max_tokens_dia": 1_200,
        "llm_max_tokens": 600,
        "nl_sql_audit_enabled": False,
    }
    base.update(extra)
    return Settings(**base)


def limitador(coleccion: ColeccionFalsa, settings: Settings) -> limites.Limitador:
    return limites.limitador_para(BaseFalsa(coleccion), settings)


def reservar(limitador_llm: limites.Limitador, settings: Settings, visitante: str = "abc"):
    return limitador_llm.reservar(
        clave_visitante=visitante, settings=settings, costo_tokens=settings.llm_max_tokens
    )


def test_limite_por_minuto_por_visitante() -> None:
    cola = ColeccionFalsa()
    settings = ajustes()
    lim = limitador(cola, settings)
    reservar(lim, settings)
    reservar(lim, settings)
    with pytest.raises(limites.CuotaExcedida, match="un minuto"):
        reservar(lim, settings)


def test_limite_diario_por_visitante() -> None:
    cola = ColeccionFalsa()
    settings = ajustes(llm_consultas_por_minuto=10, llm_max_solicitudes_por_ip_dia=2, llm_max_tokens_dia=50_000)
    lim = limitador(cola, settings)
    reservar(lim, settings)
    reservar(lim, settings)
    with pytest.raises(limites.CuotaExcedida, match="máximo de consultas por día"):
        reservar(lim, settings)
    # Otro visitante conserva su propio cupo.
    reservar(lim, settings, visitante="otro")


def test_limite_global_de_solicitudes_y_tokens() -> None:
    cola = ColeccionFalsa()
    settings = ajustes(
        llm_consultas_por_minuto=10,
        llm_max_solicitudes_por_ip_dia=50,
        llm_max_solicitudes_dia=2,
    )
    lim = limitador(cola, settings)
    reservar(lim, settings, visitante="a")
    reservar(lim, settings, visitante="b")
    with pytest.raises(limites.CuotaExcedida, match="máximo diario de consultas"):
        reservar(lim, settings, visitante="c")

    cola2 = ColeccionFalsa()
    settings2 = ajustes(
        llm_consultas_por_minuto=10,
        llm_max_solicitudes_por_ip_dia=50,
        llm_max_solicitudes_dia=50,
        llm_max_tokens_dia=1_000,
    )
    lim2 = limitador(cola2, settings2)
    reservar(lim2, settings2, visitante="a")  # 600 tokens reservados
    with pytest.raises(limites.CuotaExcedida, match="presupuesto diario de tokens"):
        reservar(lim2, settings2, visitante="b")


def test_falla_cerrado_si_el_almacen_no_responde() -> None:
    settings = ajustes()
    lim = limitador(ColeccionFalsa(falla=True), settings)
    with pytest.raises(limites.LimitesNoDisponibles):
        reservar(lim, settings)


def test_estado_expone_solo_contadores() -> None:
    cola = ColeccionFalsa()
    settings = ajustes()
    lim = limitador(cola, settings)
    reservar(lim, settings)
    estado = lim.estado()
    assert set(estado) == {"solicitudes_hoy", "tokens_reservados_hoy"}
    assert estado["solicitudes_hoy"] == 1
    assert "abc" not in json.dumps(estado)


def test_ip_real_solo_confia_en_proxies_declarados() -> None:
    settings = ajustes()
    cabeceras = {"x-real-ip": "203.0.113.9", "x-forwarded-for": "203.0.113.9, 10.0.0.2"}
    # Conexión directa del visitante: se ignora la cabecera enviada por el cliente.
    assert limites.ip_real("198.51.100.7", cabeceras, settings) == "198.51.100.7"
    # Conexión desde el proxy del despliegue: se acepta su cabecera.
    assert limites.ip_real("172.18.0.4", cabeceras, settings) == "203.0.113.9"
    # Cabecera inválida: se conserva la IP de la conexión.
    assert (
        limites.ip_real("172.18.0.4", {"x-real-ip": "no-es-ip"}, settings) == "172.18.0.4"
    )


def test_clave_de_visitante_derivada_y_con_vencimiento() -> None:
    settings = ajustes()
    clave_hoy = limites.clave_ip("203.0.113.9", "2026-11-22", settings)
    assert len(clave_hoy) == 16
    assert "203.0.113.9" not in clave_hoy
    assert clave_hoy == limites.clave_ip("203.0.113.9", "2026-11-22", settings)
    assert clave_hoy != limites.clave_ip("203.0.113.9", "2026-11-23", settings)
    assert clave_hoy != limites.clave_ip("203.0.113.10", "2026-11-22", settings)


def test_todas_las_rutas_llm_llevan_el_guardian() -> None:
    app = create_app(Settings(_env_file=None))
    rutas_llm = {
        "/api/v1/incident-commander/runs/{run_id}/analysis",
        "/api/v1/kilometro-cero/analisis",
        "/api/v1/ripple-room/analisis",
        "/api/v1/dockmind/analisis",
        "/api/v1/briefing-banxico/query",
        "/api/v1/copiloto-nl-sql/query",
    }
    vistas = set()
    for ruta in app.routes:
        camino = getattr(ruta, "path", None)
        if camino not in rutas_llm:
            continue
        vistas.add(camino)
        llamadas = [dependencia.call for dependencia in ruta.dependant.dependencies]
        assert exigir_cuota_llm in llamadas, f"{camino} no reserva cuota LLM"
    assert vistas == rutas_llm, f"faltan rutas: {rutas_llm - vistas}"


def test_tope_de_cuerpo_responde_413() -> None:
    app = FastAPI()
    app.add_middleware(LimiteCuerpoMiddleware, maximo=128)

    @app.post("/analisis")
    def analisis(payload: dict) -> dict:  # pragma: no cover - no debe llegar
        return payload

    cliente_http = TestClient(app)
    assert cliente_http.post("/analisis", json={"a": 1}).status_code == 200
    grande = cliente_http.post("/analisis", content=b"x" * 200, headers={"content-type": "application/json"})
    assert grande.status_code == 413
    assert "demasiado grande" in grande.json()["detail"]


def test_nl_sql_usa_el_cliente_compartido(monkeypatch) -> None:
    from app.nl_sql.service import CopilotoNlSql

    capturado = {}

    def falso_post_chat(settings, payload, *, timeout=None):
        capturado["payload"] = payload
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "country_code": "MEX",
                                "indicator_codes": ["FP.CPI.TOTL.ZG"],
                                "start_year": 2019,
                                "end_year": 2025,
                                "operation": "trend",
                            }
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 120},
        }

    monkeypatch.setattr(cliente, "post_chat", falso_post_chat)
    cliente.VENTANA.reiniciar()
    settings = ajustes(llm_consultas_por_minuto=120)
    servicio = CopilotoNlSql(None, settings)
    servicio._auditar = lambda *args, **kwargs: None  # sin Mongo en esta prueba
    resultado = servicio.consultar("¿Cómo evolucionó la inflación?")
    assert resultado["intent_mode"] == "llm-json-validado"
    # La construcción SQL y el modo de solo lectura se cubren en
    # tests/test_copiloto_nl_sql.py; aquí solo se comprueba el transporte.
    assert resultado["spec"]["indicator_codes"] == ["FP.CPI.TOTL.ZG"]
    assert "DELETE" not in str(resultado).upper()
    payload = capturado["payload"]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_tokens"] <= 400
    assert "clave-ficticia-de-prueba" not in json.dumps(payload)
