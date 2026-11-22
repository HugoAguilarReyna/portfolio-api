"""Telemetría anónima: contadores globales, unicidad diaria y ranking.

La mitad de las pruebas usa un doble de la colección (sin MongoDB); las de
integración usan la base aislada ``hugo_portfolio_test`` que configura
``tests/conftest.py``. En ningún caso se guarda la IP ni el `User-Agent`.
"""

from __future__ import annotations

import copy
import re
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pymongo import ASCENDING

from app.config import Settings, get_settings
from app.db.indexes import INDEX_SPECS
from app.db.mongo import METRICAS_VISITAS
from app.llm import limites
from app.main import create_app
from app.services.visitas_service import (
    MAXIMO_TOP,
    ServicioVisitas,
    es_bot,
    huella_diaria,
)

CLAVE = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
UA_HUMANO = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
UA_BOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
IP_PRUEBA = "203.0.113.7"


# ------------------------------------------------------------------ dobles
def _incrementar(documento: dict, campo: str, valor: int) -> None:
    partes = campo.split(".")
    actual = documento
    for parte in partes[:-1]:
        actual = actual.setdefault(parte, {})
    actual[partes[-1]] = int(actual.get(partes[-1], 0)) + int(valor)


class ColeccionFalsa:
    """Imita las operaciones atómicas que usa el servicio, con candado."""

    def __init__(self) -> None:
        self.documentos: dict[str, dict] = {}
        self.candado = threading.Lock()
        self.indices: list[tuple[tuple, dict]] = []

    def update_one(self, filtro, actualizacion, upsert=False):
        with self.candado:
            clave = filtro["_id"]
            documento = self.documentos.get(clave)
            creado = documento is None
            if creado:
                if not upsert:
                    return SimpleNamespace(upserted_id=None)
                documento = {"_id": clave}
                self.documentos[clave] = documento
            for campo, valor in (actualizacion.get("$setOnInsert") or {}).items():
                documento.setdefault(campo, valor)
            return SimpleNamespace(upserted_id=clave if creado else None)

    def find_one_and_update(self, filtro, actualizacion, upsert=False, return_document=None):
        with self.candado:
            clave = filtro["_id"]
            documento = self.documentos.get(clave)
            if documento is None:
                if not upsert:
                    return None
                documento = {"_id": clave}
                self.documentos[clave] = documento
            for campo, valor in (actualizacion.get("$inc") or {}).items():
                _incrementar(documento, campo, valor)
            for campo, valor in (actualizacion.get("$setOnInsert") or {}).items():
                documento.setdefault(campo, valor)
            return copy.deepcopy(documento)

    def find_one(self, filtro):
        with self.candado:
            documento = self.documentos.get(filtro["_id"])
            return copy.deepcopy(documento) if documento else None

    def create_index(self, *args, **kwargs):
        self.indices.append((args, kwargs))
        return "metricas_visitas_ttl"


class BaseFalsa:
    def __init__(self, coleccion: ColeccionFalsa) -> None:
        self.coleccion = coleccion

    def __getitem__(self, nombre: str):
        return self.coleccion


@pytest.fixture()
def ajustes(settings: Settings) -> Settings:
    return settings.model_copy(update={"llm_cuota_hmac_key": CLAVE})


@pytest.fixture()
def servicio(ajustes: Settings):
    coleccion = ColeccionFalsa()
    return ServicioVisitas(BaseFalsa(coleccion), ajustes), coleccion


# ------------------------------------------------------------------ unidad
@pytest.mark.parametrize(
    "agente",
    [
        None,
        "",
        "   ",
        UA_BOT,
        "Mozilla/5.0 (compatible; bingbot/2.0)",
        "curl/8.4.0",
        "Wget/1.21.4",
        "python-requests/2.31.0",
        "UptimeRobot/2.0",
        "Pingdom Monitoring",
        "Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)",
    ],
)
def test_detecta_bots_y_agentes_vacios(agente) -> None:
    assert es_bot(agente) is True


@pytest.mark.parametrize(
    "agente",
    [
        UA_HUMANO,
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148",
    ],
)
def test_acepta_navegadores_humanos(agente) -> None:
    assert es_bot(agente) is False


def test_huella_diaria_es_anonima_y_estable(ajustes: Settings) -> None:
    huella = huella_diaria(IP_PRUEBA, UA_HUMANO, ajustes, dia="2026-11-22")
    assert huella.startswith("v:2026-11-22:")
    assert re.fullmatch(r"v:\d{4}-\d{2}-\d{2}:[0-9a-f]{32}", huella)
    assert huella == huella_diaria(IP_PRUEBA, UA_HUMANO, ajustes, dia="2026-11-22")
    assert IP_PRUEBA not in huella
    assert "Mozilla" not in huella
    assert huella != huella_diaria("203.0.113.8", UA_HUMANO, ajustes, dia="2026-11-22")
    assert huella != huella_diaria(IP_PRUEBA, UA_BOT, ajustes, dia="2026-11-22")
    assert huella != huella_diaria(IP_PRUEBA, UA_HUMANO, ajustes, dia="2026-11-23")


def test_huella_recorta_el_user_agent_a_120(ajustes: Settings) -> None:
    largo = "A" * 120 + "B" * 200
    assert huella_diaria(IP_PRUEBA, largo, ajustes, dia="2026-11-22") == huella_diaria(
        IP_PRUEBA, "A" * 120, ajustes, dia="2026-11-22"
    )


def test_huella_sin_clave_falla_cerrado(ajustes: Settings) -> None:
    sin_clave = ajustes.model_copy(update={"llm_cuota_hmac_key": None})
    with pytest.raises(limites.LimitesNoConfigurados):
        huella_diaria(IP_PRUEBA, UA_HUMANO, sin_clave)


def test_registrar_unicos_y_totales(servicio, ajustes: Settings) -> None:
    telemetria, coleccion = servicio
    primero = telemetria.registrar(ip=IP_PRUEBA, user_agent=UA_HUMANO)
    assert primero == {
        "visitas_totales": 1,
        "visitantes_unicos": 1,
        "contabilizada": True,
    }
    segundo = telemetria.registrar(ip=IP_PRUEBA, user_agent=UA_HUMANO)
    assert segundo == {
        "visitas_totales": 2,
        "visitantes_unicos": 1,
        "contabilizada": True,
    }
    tercero = telemetria.registrar(
        ip=IP_PRUEBA, user_agent=UA_HUMANO.replace("Chrome/120", "Firefox/121")
    )
    assert tercero == {
        "visitas_totales": 3,
        "visitantes_unicos": 2,
        "contabilizada": True,
    }
    # Solo hay dos documentos de huella, uno por combinación IP + UA del día.
    huellas = [clave for clave in coleccion.documentos if clave != "global"]
    assert len(huellas) == 2
    assert all(clave.startswith("v:") for clave in huellas)


def test_registrar_bot_no_incrementa(servicio) -> None:
    telemetria, coleccion = servicio
    telemetria.registrar(ip=IP_PRUEBA, user_agent=UA_HUMANO)
    for agente in (UA_BOT, "", "curl/8.4.0"):
        respuesta = telemetria.registrar(ip=IP_PRUEBA, user_agent=agente)
        assert respuesta == {
            "visitas_totales": 1,
            "visitantes_unicos": 1,
            "contabilizada": False,
        }
    assert len(coleccion.documentos) == 2  # global + una huella humana


def test_proyectos_y_ranking(servicio) -> None:
    telemetria, coleccion = servicio
    for _ in range(3):
        telemetria.registrar_proyecto(
            proyecto_id="data-incident-commander",
            area_id="agentes",
            user_agent=UA_HUMANO,
        )
    telemetria.registrar_proyecto(
        proyecto_id="atlas-vial", area_id="dashboards", user_agent=UA_HUMANO
    )
    telemetria.registrar_proyecto(
        proyecto_id="atlas-vial", area_id="dashboards", user_agent=UA_HUMANO
    )
    telemetria.registrar_proyecto(
        proyecto_id="pulso-comercial", area_id=None, user_agent=UA_HUMANO
    )

    estado = telemetria.estado()
    assert estado["top_proyectos"] == [
        {"proyecto_id": "data-incident-commander", "vistas": 3},
        {"proyecto_id": "atlas-vial", "vistas": 2},
        {"proyecto_id": "pulso-comercial", "vistas": 1},
    ]
    assert [fila["proyecto_id"] for fila in estado["top_proyectos"]] == [
        "data-incident-commander",
        "atlas-vial",
        "pulso-comercial",
    ]
    global_doc = coleccion.documentos["global"]
    assert global_doc["proyectos"] == {
        "data-incident-commander": 3,
        "atlas-vial": 2,
        "pulso-comercial": 1,
    }
    assert global_doc["areas"] == {"agentes": 3, "dashboards": 2}


def test_proyecto_bot_no_incrementa(servicio) -> None:
    telemetria, _ = servicio
    respuesta = telemetria.registrar_proyecto(
        proyecto_id="atlas-vial", area_id="dashboards", user_agent=UA_BOT
    )
    assert respuesta == {"ok": True, "proyecto_id": "atlas-vial", "vistas_proyecto": 0}
    assert telemetria.estado()["top_proyectos"] == []


def test_ranking_estable_y_acotado(servicio) -> None:
    telemetria, _ = servicio
    for indice in range(MAXIMO_TOP + 5):
        for _ in range(indice % 3):
            telemetria.registrar_proyecto(
                proyecto_id=f"proyecto-{indice:02d}",
                area_id=None,
                user_agent=UA_HUMANO,
            )
    telemetria.registrar_proyecto(
        proyecto_id="proyecto-00", area_id=None, user_agent=UA_HUMANO
    )
    ranking = telemetria.estado()["top_proyectos"]
    assert len(ranking) == MAXIMO_TOP
    vistas = [fila["vistas"] for fila in ranking]
    assert vistas == sorted(vistas, reverse=True)
    # Cinco proyectos con dos vistas; el desempate alfabético los ordena.
    assert [fila["proyecto_id"] for fila in ranking[:5]] == [
        "proyecto-02",
        "proyecto-05",
        "proyecto-08",
        "proyecto-11",
        "proyecto-14",
    ]
    # El primero con una vista es proyecto-00 (desempate alfabético estable).
    assert ranking[5] == {"proyecto_id": "proyecto-00", "vistas": 1}


def test_ttl_declarado_en_los_indices() -> None:
    especificaciones = {
        (coleccion, nombre): (claves, opciones)
        for coleccion, nombre, claves, opciones in INDEX_SPECS
    }
    claves, opciones = especificaciones[(METRICAS_VISITAS, "metricas_visitas_ttl")]
    assert claves == [("expira_en", ASCENDING)]
    assert opciones["expireAfterSeconds"] == 0


# ------------------------------------------------------------ integración
@pytest.fixture()
def entorno_visitas(settings: Settings, mongo_db):
    """Aplicación real con MongoDB aislado y la clave HMAC de pruebas."""
    db = mongo_db
    db[METRICAS_VISITAS].delete_many({})
    ajustes = settings.model_copy(update={"llm_cuota_hmac_key": CLAVE})
    app = create_app(ajustes)
    app.dependency_overrides[get_settings] = lambda: ajustes
    with TestClient(app) as cliente:
        yield cliente, db
    db[METRICAS_VISITAS].delete_many({})


def test_http_estado_inicial(entorno_visitas) -> None:
    cliente, _ = entorno_visitas
    respuesta = cliente.get("/api/v1/visitas")
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "visitas_totales": 0,
        "visitantes_unicos": 0,
        "top_proyectos": [],
    }


def test_http_una_visita_por_navegador_y_dia(entorno_visitas) -> None:
    cliente, db = entorno_visitas
    cabeceras = {"User-Agent": UA_HUMANO}
    primera = cliente.post("/api/v1/visitas/registrar", headers=cabeceras)
    assert primera.status_code == 200
    assert primera.json() == {
        "visitas_totales": 1,
        "visitantes_unicos": 1,
        "contabilizada": True,
    }
    segunda = cliente.post("/api/v1/visitas/registrar", headers=cabeceras)
    assert segunda.json() == {
        "visitas_totales": 2,
        "visitantes_unicos": 1,
        "contabilizada": True,
    }
    otro = cliente.post(
        "/api/v1/visitas/registrar",
        headers={"User-Agent": UA_HUMANO.replace("Chrome/120", "Edge/120")},
    )
    assert otro.json() == {
        "visitas_totales": 3,
        "visitantes_unicos": 2,
        "contabilizada": True,
    }
    # GET no incrementa nada.
    estado = cliente.get("/api/v1/visitas").json()
    assert estado["visitas_totales"] == 3
    assert estado["visitantes_unicos"] == 2
    assert estado["top_proyectos"] == []
    assert cliente.get("/api/v1/visitas").json() == estado
    assert db[METRICAS_VISITAS].count_documents({}) == 3  # global + 2 huellas


def test_http_bot_no_incrementa(entorno_visitas) -> None:
    cliente, db = entorno_visitas
    respuesta = cliente.post(
        "/api/v1/visitas/registrar", headers={"User-Agent": UA_BOT}
    )
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "visitas_totales": 0,
        "visitantes_unicos": 0,
        "contabilizada": False,
    }
    vacio = cliente.post("/api/v1/visitas/registrar", headers={"User-Agent": ""})
    assert vacio.json()["contabilizada"] is False
    assert db[METRICAS_VISITAS].count_documents({}) == 0


def test_http_proyectos_y_ranking(entorno_visitas) -> None:
    cliente, db = entorno_visitas
    cabeceras = {"User-Agent": UA_HUMANO}
    for esperado in (1, 2, 3):
        respuesta = cliente.post(
            "/api/v1/visitas/proyecto",
            json={"proyecto_id": "data-incident-commander", "area_id": "agentes"},
            headers=cabeceras,
        )
        assert respuesta.status_code == 200
        assert respuesta.json()["vistas_proyecto"] == esperado
    ultima = cliente.post(
        "/api/v1/visitas/proyecto",
        json={"proyecto_id": "atlas-vial", "area_id": "dashboards"},
        headers=cabeceras,
    )
    assert ultima.json() == {
        "ok": True,
        "proyecto_id": "atlas-vial",
        "vistas_proyecto": 1,
    }
    ranking = cliente.get("/api/v1/visitas").json()["top_proyectos"]
    assert ranking == [
        {"proyecto_id": "data-incident-commander", "vistas": 3},
        {"proyecto_id": "atlas-vial", "vistas": 1},
    ]
    global_doc = db[METRICAS_VISITAS].find_one({"_id": "global"})
    assert global_doc["areas"] == {"agentes": 3, "dashboards": 1}
    assert global_doc.get("visitas_totales", 0) == 0


def test_http_proyecto_bot_y_validacion(entorno_visitas) -> None:
    cliente, _ = entorno_visitas
    respuesta = cliente.post(
        "/api/v1/visitas/proyecto",
        json={"proyecto_id": "atlas-vial"},
        headers={"User-Agent": UA_BOT},
    )
    assert respuesta.json() == {
        "ok": True,
        "proyecto_id": "atlas-vial",
        "vistas_proyecto": 0,
    }
    for cuerpo in (
        {},
        {"proyecto_id": "x"},
        {"proyecto_id": "con espacio"},
        {"proyecto_id": "x" * 65},
        {"proyecto_id": "atlas-vial", "area_id": "no_válido"},
        {"proyecto_id": "atlas-vial", "extra": 1},
    ):
        invalido = cliente.post(
            "/api/v1/visitas/proyecto", json=cuerpo, headers={"User-Agent": UA_HUMANO}
        )
        assert invalido.status_code == 422, cuerpo

    valido = cliente.post(
        "/api/v1/visitas/proyecto",
        json={"proyecto_id": "atlas-vial"},
        headers={"User-Agent": UA_HUMANO},
    )
    assert valido.status_code == 200


def test_http_privacidad_y_ttl(entorno_visitas) -> None:
    cliente, db = entorno_visitas
    cliente.post(
        "/api/v1/visitas/registrar", headers={"User-Agent": UA_HUMANO}
    )
    documentos = list(db[METRICAS_VISITAS].find({}))
    assert len(documentos) == 2
    huella = next(doc for doc in documentos if doc["_id"] != "global")
    assert re.fullmatch(r"v:\d{4}-\d{2}-\d{2}:[0-9a-f]{32}", huella["_id"])
    assert "testclient" not in huella["_id"]
    assert "Mozilla" not in repr(huella)
    assert "203.0.113" not in repr(huella)
    ahora = datetime.now(timezone.utc).replace(tzinfo=None)
    assert huella["dia"] == ahora.strftime("%Y-%m-%d")
    assert ahora + timedelta(hours=47) < huella["expira_en"] < ahora + timedelta(hours=49)

    global_doc = next(doc for doc in documentos if doc["_id"] == "global")
    assert "expira_en" not in global_doc
    assert global_doc["visitas_totales"] == 1
    assert global_doc["visitantes_unicos"] == 1

    indices = db[METRICAS_VISITAS].index_information()
    assert indices["metricas_visitas_ttl"]["expireAfterSeconds"] == 0
    assert indices["metricas_visitas_ttl"]["key"] == [("expira_en", ASCENDING)]


def test_http_sin_clave_falla_cerrado(settings: Settings) -> None:
    """Sin LLM_CUOTA_HMAC_KEY no se puede identificar al visitante: 503."""
    sin_clave = settings.model_copy(update={"llm_cuota_hmac_key": None})
    app = create_app(sin_clave)
    app.dependency_overrides[get_settings] = lambda: sin_clave
    with TestClient(app) as cliente:
        respuesta = cliente.post(
            "/api/v1/visitas/registrar", headers={"User-Agent": UA_HUMANO}
        )
        assert respuesta.status_code == 503
        assert "LLM_CUOTA_HMAC_KEY" in respuesta.json()["detail"]
        # Un bot no necesita la clave porque no deja huella.
        bot = cliente.post(
            "/api/v1/visitas/registrar", headers={"User-Agent": UA_BOT}
        )
        assert bot.status_code == 200
        assert bot.json()["contabilizada"] is False
