"""Telemetría anónima: catálogo cerrado, dedupe diario y límite de frecuencia.

La mitad de las pruebas usa un doble de las colecciones (sin MongoDB); las de
integración usan la base aislada ``hugo_portfolio_test`` que configura
``tests/conftest.py``. En ningún caso se guarda la IP ni el `User-Agent`.

Sincronización del catálogo (repos separados; no se puede regenerar en CI)::

    cd portafolio-hugo-aguilar
    node tools/exportar-catalogo-ids.mjs \\
        --salida ../portfolio-api/app/data/catalogo_ids.json

La prueba de formato verifica que el JSON de la API sea consistente; la de
contrato usa el archivo real para rechazar identificadores desconocidos.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pymongo import ASCENDING

from app.config import Settings, get_settings
from app.db.indexes import INDEX_SPECS
from app.db.mongo import METRICAS_VISITAS, METRICAS_VISITAS_LIMITES
from app.llm import limites
from app.main import create_app
from app.services.catalogo_ids import RUTA_CATALOGO, catalogo_ids
from app.services.visitas_service import (
    MAXIMO_TOP,
    AreaDesconocida,
    AreaNoCorresponde,
    LimiteFrecuencia,
    ProyectoDesconocido,
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
    """Imita `update_one`, `find_one_and_update` y `find_one` con candado.

    Cubre los filtros que usa el servicio: `_id`, `proyectos: {$ne: id}` y
    `proyectos.<n>: {$exists: false}`.
    """

    def __init__(self) -> None:
        self.documentos: dict[str, dict] = {}
        self.candado = threading.Lock()
        self.indices: list[tuple[tuple, dict]] = []

    @staticmethod
    def _coincide(documento: dict, filtro: dict) -> bool:
        for campo, esperado in filtro.items():
            if campo == "_id":
                if documento.get("_id") != esperado:
                    return False
                continue
            if campo == "proyectos" and isinstance(esperado, dict) and "$ne" in esperado:
                valor = documento.get("proyectos")
                if isinstance(valor, list) and esperado["$ne"] in valor:
                    return False
                if valor == esperado["$ne"]:
                    return False
                continue
            if campo.startswith("proyectos.") and isinstance(esperado, dict):
                indice = int(campo.split(".", 1)[1])
                valor = documento.get("proyectos")
                if esperado.get("$exists") is False:
                    if isinstance(valor, list) and len(valor) > indice:
                        return False
                    continue
        return True

    def update_one(self, filtro, actualizacion, upsert=False):
        with self.candado:
            clave = filtro.get("_id")
            documento = self.documentos.get(clave) if clave is not None else None
            creado = documento is None
            if creado:
                if not upsert:
                    return SimpleNamespace(upserted_id=None, modified_count=0)
                documento = {"_id": clave}
                self.documentos[clave] = documento
            if not self._coincide(documento, filtro):
                return SimpleNamespace(upserted_id=None, modified_count=0)
            for campo, valor in (actualizacion.get("$setOnInsert") or {}).items():
                documento.setdefault(campo, valor)
            modificado = False
            for campo, valor in (actualizacion.get("$addToSet") or {}).items():
                lista = documento.setdefault(campo, [])
                if valor not in lista:
                    lista.append(valor)
                    modificado = True
            return SimpleNamespace(
                upserted_id=clave if creado else None,
                modified_count=1 if modificado else 0,
            )

    def find_one_and_update(self, filtro, actualizacion, upsert=False, return_document=None):
        with self.candado:
            clave = filtro.get("_id")
            documento = self.documentos.get(clave) if clave is not None else None
            if documento is None:
                if not upsert:
                    return None
                documento = {"_id": clave}
                self.documentos[clave] = documento
            if not self._coincide(documento, filtro):
                return None
            for campo, valor in (actualizacion.get("$inc") or {}).items():
                _incrementar(documento, campo, valor)
            for campo, valor in (actualizacion.get("$setOnInsert") or {}).items():
                documento.setdefault(campo, valor)
            return copy.deepcopy(documento)

    def find_one(self, filtro):
        with self.candado:
            documento = self.documentos.get(filtro.get("_id"))
            return copy.deepcopy(documento) if documento else None

    def create_index(self, *args, **kwargs):
        self.indices.append((args, kwargs))
        return "metricas_visitas_ttl"


class BaseFalsa:
    """Base con dos colecciones: telemetría y contadores de frecuencia."""

    def __init__(
        self,
        coleccion: ColeccionFalsa,
        limites_coleccion: ColeccionFalsa | None = None,
    ) -> None:
        self.coleccion = coleccion
        self.limites_coleccion = limites_coleccion or ColeccionFalsa()

    def __getitem__(self, nombre: str):
        if nombre == METRICAS_VISITAS_LIMITES:
            return self.limites_coleccion
        return self.coleccion


@pytest.fixture()
def ajustes(settings: Settings) -> Settings:
    # Límites holgados para las pruebas de lógica; la prueba de frecuencia usa
    # un valor bajo explícito.
    return settings.model_copy(
        update={
            "llm_cuota_hmac_key": CLAVE,
            "telemetria_max_peticiones_minuto": 200,
            "telemetria_max_proyectos_huella": 30,
        }
    )


@pytest.fixture()
def servicio(ajustes: Settings):
    coleccion = ColeccionFalsa()
    limites_coleccion = ColeccionFalsa()
    return (
        ServicioVisitas(BaseFalsa(coleccion, limites_coleccion), ajustes),
        coleccion,
        limites_coleccion,
    )


def _registrado(telemetria: ServicioVisitas) -> None:
    telemetria.registrar(ip=IP_PRUEBA, user_agent=UA_HUMANO)


# ------------------------------------------------------------------ catálogo
def test_catalogo_json_formato() -> None:
    crudo = json.loads(RUTA_CATALOGO.read_text(encoding="utf-8"))
    assert crudo["generado_desde"] == "dist/content.js"
    assert crudo["proyectos"] == sorted(crudo["proyectos"])
    assert crudo["areas"] == sorted(crudo["areas"])
    assert len(crudo["proyectos"]) == len(set(crudo["proyectos"]))
    assert set(crudo["proyecto_area"]) == set(crudo["proyectos"])
    for area in crudo["proyecto_area"].values():
        assert area in crudo["areas"]
    for valor in crudo["proyectos"] + crudo["areas"]:
        assert re.fullmatch(r"[a-zA-Z0-9-]{2,64}", valor)
    catalogo = catalogo_ids()
    assert catalogo.proyectos == frozenset(crudo["proyectos"])
    assert catalogo.areas == frozenset(crudo["areas"])


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


def test_registrar_unicos_y_totales(servicio) -> None:
    telemetria, coleccion, _ = servicio
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
    huellas = [clave for clave in coleccion.documentos if clave != "global"]
    assert len(huellas) == 2
    assert all(clave.startswith("v:") for clave in huellas)


def test_registrar_bot_no_incrementa(servicio) -> None:
    telemetria, coleccion, limites_coleccion = servicio
    _registrado(telemetria)
    for agente in (UA_BOT, "", "curl/8.4.0"):
        respuesta = telemetria.registrar(ip=IP_PRUEBA, user_agent=agente)
        assert respuesta == {
            "visitas_totales": 1,
            "visitantes_unicos": 1,
            "contabilizada": False,
        }
    assert len(coleccion.documentos) == 2  # global + una huella humana
    # Un bot no deja huella; el único contador de frecuencia es el del humano.
    assert len(limites_coleccion.documentos) == 1


def test_mismo_proyecto_dos_veces_cuenta_uno(servicio) -> None:
    telemetria, coleccion, _ = servicio
    _registrado(telemetria)
    primera = telemetria.registrar_proyecto(
        proyecto_id="data-incident-commander",
        area_id="agentes",
        ip=IP_PRUEBA,
        user_agent=UA_HUMANO,
    )
    segunda = telemetria.registrar_proyecto(
        proyecto_id="data-incident-commander",
        area_id="agentes",
        ip=IP_PRUEBA,
        user_agent=UA_HUMANO,
    )
    assert primera["vistas_proyecto"] == 1
    assert segunda["vistas_proyecto"] == 1
    assert coleccion.documentos["global"]["proyectos"] == {
        "data-incident-commander": 1
    }
    huella = next(clave for clave in coleccion.documentos if clave.startswith("v:"))
    assert coleccion.documentos[huella]["proyectos"] == ["data-incident-commander"]


def test_proyecto_sin_huella_no_cuenta(servicio) -> None:
    telemetria, coleccion, _ = servicio
    respuesta = telemetria.registrar_proyecto(
        proyecto_id="atlas-vial",
        area_id="dashboards",
        ip=IP_PRUEBA,
        user_agent=UA_HUMANO,
    )
    assert respuesta == {"ok": True, "proyecto_id": "atlas-vial", "vistas_proyecto": 0}
    assert coleccion.documentos == {}
    assert telemetria.estado()["top_proyectos"] == []


def test_proyecto_desconocido_no_incrementa(servicio) -> None:
    telemetria, coleccion, _ = servicio
    _registrado(telemetria)
    antes = copy.deepcopy(coleccion.documentos["global"])
    with pytest.raises(ProyectoDesconocido):
        telemetria.registrar_proyecto(
            proyecto_id="proyecto-inventado",
            area_id=None,
            ip=IP_PRUEBA,
            user_agent=UA_HUMANO,
        )
    assert coleccion.documentos["global"] == antes


def test_area_desconocida_y_no_corresponde(servicio) -> None:
    telemetria, coleccion, _ = servicio
    _registrado(telemetria)
    with pytest.raises(AreaDesconocida):
        telemetria.registrar_proyecto(
            proyecto_id="atlas-vial",
            area_id="area-inventada",
            ip=IP_PRUEBA,
            user_agent=UA_HUMANO,
        )
    with pytest.raises(AreaNoCorresponde):
        telemetria.registrar_proyecto(
            proyecto_id="atlas-vial",
            area_id="agentes",
            ip=IP_PRUEBA,
            user_agent=UA_HUMANO,
        )
    assert "proyectos" not in coleccion.documentos["global"]


def test_inundacion_de_slugs_no_crea_claves(servicio) -> None:
    telemetria, coleccion, _ = servicio
    _registrado(telemetria)
    for indice in range(1_000):
        with pytest.raises(ProyectoDesconocido):
            telemetria.registrar_proyecto(
                proyecto_id=f"slug-{indice:04d}",
                area_id=None,
                ip=IP_PRUEBA,
                user_agent=UA_HUMANO,
            )
    assert "proyectos" not in coleccion.documentos["global"]
    assert "areas" not in coleccion.documentos["global"]


def test_tope_de_proyectos_por_huella(ajustes: Settings) -> None:
    ajustes = ajustes.model_copy(update={"telemetria_max_proyectos_huella": 3})
    coleccion = ColeccionFalsa()
    telemetria = ServicioVisitas(BaseFalsa(coleccion), ajustes)
    _registrado(telemetria)
    ids = sorted(catalogo_ids().proyectos)[:4]
    areas = {proyecto: catalogo_ids().area_de(proyecto) for proyecto in ids}
    for proyecto in ids:
        telemetria.registrar_proyecto(
            proyecto_id=proyecto,
            area_id=areas[proyecto],
            ip=IP_PRUEBA,
            user_agent=UA_HUMANO,
        )
    huella = next(clave for clave in coleccion.documentos if clave.startswith("v:"))
    assert len(coleccion.documentos[huella]["proyectos"]) == 3
    assert len(coleccion.documentos["global"]["proyectos"]) == 3


def test_limite_de_frecuencia(ajustes: Settings) -> None:
    ajustes = ajustes.model_copy(update={"telemetria_max_peticiones_minuto": 2})
    coleccion = ColeccionFalsa()
    limites_coleccion = ColeccionFalsa()
    telemetria = ServicioVisitas(BaseFalsa(coleccion, limites_coleccion), ajustes)
    _registrado(telemetria)  # 1.ª petición del minuto
    telemetria.registrar_proyecto(
        proyecto_id="atlas-vial",
        area_id="dashboards",
        ip=IP_PRUEBA,
        user_agent=UA_HUMANO,
    )  # 2.ª
    with pytest.raises(LimiteFrecuencia) as excinfo:
        telemetria.registrar_proyecto(
            proyecto_id="atlas-vial",
            area_id="dashboards",
            ip=IP_PRUEBA,
            user_agent=UA_HUMANO,
        )
    assert 1 <= excinfo.value.reintentar_en <= 60
    assert len(limites_coleccion.documentos) == 1


def test_proyecto_bot_no_incrementa(servicio) -> None:
    telemetria, _, _ = servicio
    respuesta = telemetria.registrar_proyecto(
        proyecto_id="atlas-vial",
        area_id="dashboards",
        ip=IP_PRUEBA,
        user_agent=UA_BOT,
    )
    assert respuesta == {"ok": True, "proyecto_id": "atlas-vial", "vistas_proyecto": 0}
    assert telemetria.estado()["top_proyectos"] == []


def test_ranking_estable_y_acotado(servicio) -> None:
    telemetria, _, _ = servicio
    catalogo = catalogo_ids()
    ids = sorted(catalogo.proyectos)
    # Dos visitantes distintos del mismo día: el ranking suma una vista por
    # visitante y proyecto, nunca dos del mismo visitante.
    for visitante, agente in enumerate((UA_HUMANO, UA_HUMANO + " segundo")):
        telemetria.registrar(ip=IP_PRUEBA, user_agent=agente)
        for indice, proyecto in enumerate(ids):
            if visitante == 0 or indice < 5:
                telemetria.registrar_proyecto(
                    proyecto_id=proyecto,
                    area_id=None,
                    ip=IP_PRUEBA,
                    user_agent=agente,
                )
    ranking = telemetria.estado()["top_proyectos"]
    assert len(ranking) == MAXIMO_TOP
    vistas = [fila["vistas"] for fila in ranking]
    assert vistas == sorted(vistas, reverse=True)
    # Los cinco primeros reciben dos vistas; el desempate es alfabético.
    assert [fila["proyecto_id"] for fila in ranking[:5]] == ids[:5]
    assert all(fila["vistas"] == 2 for fila in ranking[:5])


def test_ttl_declarado_en_los_indices() -> None:
    especificaciones = {
        (coleccion, nombre): (claves, opciones)
        for coleccion, nombre, claves, opciones in INDEX_SPECS
    }
    claves, opciones = especificaciones[(METRICAS_VISITAS, "metricas_visitas_ttl")]
    assert claves == [("expira_en", ASCENDING)]
    assert opciones["expireAfterSeconds"] == 0
    claves, opciones = especificaciones[
        (METRICAS_VISITAS_LIMITES, "metricas_visitas_limites_ttl")
    ]
    assert claves == [("expira_en", ASCENDING)]
    assert opciones["expireAfterSeconds"] == 0


# ------------------------------------------------------------ integración
def _app_visitas(settings: Settings, **overrides):
    ajustes = settings.model_copy(
        update={
            "llm_cuota_hmac_key": CLAVE,
            "telemetria_max_peticiones_minuto": 200,
            **overrides,
        }
    )
    app = create_app(ajustes)
    app.dependency_overrides[get_settings] = lambda: ajustes
    return app


@pytest.fixture()
def entorno_visitas(settings: Settings, mongo_db):
    """Aplicación real con MongoDB aislado y la clave HMAC de pruebas."""
    db = mongo_db
    db[METRICAS_VISITAS].delete_many({})
    db[METRICAS_VISITAS_LIMITES].delete_many({})
    app = _app_visitas(settings)
    with TestClient(app) as cliente:
        yield cliente, db
    db[METRICAS_VISITAS].delete_many({})
    db[METRICAS_VISITAS_LIMITES].delete_many({})


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
    registro = cliente.post("/api/v1/visitas/registrar", headers=cabeceras)
    assert registro.status_code == 200
    for _ in range(3):
        respuesta = cliente.post(
            "/api/v1/visitas/proyecto",
            json={"proyecto_id": "data-incident-commander", "area_id": "agentes"},
            headers=cabeceras,
        )
        assert respuesta.status_code == 200
        assert respuesta.json()["vistas_proyecto"] == 1  # solo la primera cuenta
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
        {"proyecto_id": "atlas-vial", "vistas": 1},
        {"proyecto_id": "data-incident-commander", "vistas": 1},
    ]
    global_doc = db[METRICAS_VISITAS].find_one({"_id": "global"})
    assert global_doc["areas"] == {"agentes": 1, "dashboards": 1}
    assert global_doc.get("visitas_totales") == 1
    huella = db[METRICAS_VISITAS].find_one(
        {"_id": {"$regex": "^v:"}}, {"proyectos": 1}
    )
    assert sorted(huella["proyectos"]) == ["atlas-vial", "data-incident-commander"]


def test_http_proyecto_sin_registro_no_cuenta(entorno_visitas) -> None:
    cliente, db = entorno_visitas
    respuesta = cliente.post(
        "/api/v1/visitas/proyecto",
        json={"proyecto_id": "atlas-vial", "area_id": "dashboards"},
        headers={"User-Agent": UA_HUMANO},
    )
    assert respuesta.status_code == 200
    assert respuesta.json()["vistas_proyecto"] == 0
    assert db[METRICAS_VISITAS].count_documents({}) == 0
    assert cliente.get("/api/v1/visitas").json()["top_proyectos"] == []


def test_http_proyecto_catalogo_cerrado(entorno_visitas) -> None:
    cliente, db = entorno_visitas
    cabeceras = {"User-Agent": UA_HUMANO}
    cliente.post("/api/v1/visitas/registrar", headers=cabeceras)
    for cuerpo in (
        {"proyecto_id": "proyecto-inventado"},
        {"proyecto_id": "atlas-vial", "area_id": "area-inventada"},
        {"proyecto_id": "atlas-vial", "area_id": "agentes"},
    ):
        respuesta = cliente.post(
            "/api/v1/visitas/proyecto", json=cuerpo, headers=cabeceras
        )
        assert respuesta.status_code == 422, cuerpo
    assert db[METRICAS_VISITAS].find_one({"_id": "global"}).get("proyectos") is None


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


def test_http_limite_de_frecuencia(settings: Settings, mongo_db) -> None:
    db = mongo_db
    db[METRICAS_VISITAS].delete_many({})
    db[METRICAS_VISITAS_LIMITES].delete_many({})
    app = _app_visitas(settings, telemetria_max_peticiones_minuto=2)
    try:
        with TestClient(app) as cliente:
            cabeceras = {"User-Agent": UA_HUMANO}
            assert cliente.post("/api/v1/visitas/registrar", headers=cabeceras).status_code == 200
            assert cliente.post(
                "/api/v1/visitas/proyecto",
                json={"proyecto_id": "atlas-vial", "area_id": "dashboards"},
                headers=cabeceras,
            ).status_code == 200
            excedido = cliente.post(
                "/api/v1/visitas/proyecto",
                json={"proyecto_id": "atlas-vial", "area_id": "dashboards"},
                headers=cabeceras,
            )
            assert excedido.status_code == 429
            assert 1 <= int(excedido.headers["Retry-After"]) <= 60
    finally:
        db[METRICAS_VISITAS].delete_many({})
        db[METRICAS_VISITAS_LIMITES].delete_many({})


def test_http_privacidad_y_ttl(entorno_visitas) -> None:
    cliente, db = entorno_visitas
    cliente.post("/api/v1/visitas/registrar", headers={"User-Agent": UA_HUMANO})
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

    limite_doc = db[METRICAS_VISITAS_LIMITES].find_one({})
    assert limite_doc is not None
    assert "203.0.113" not in repr(limite_doc)
    assert "Mozilla" not in repr(limite_doc)
    indices_limite = db[METRICAS_VISITAS_LIMITES].index_information()
    assert indices_limite["metricas_visitas_limites_ttl"]["expireAfterSeconds"] == 0


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
        proyecto = cliente.post(
            "/api/v1/visitas/proyecto",
            json={"proyecto_id": "atlas-vial"},
            headers={"User-Agent": UA_HUMANO},
        )
        assert proyecto.status_code == 503
        # Un bot no necesita la clave porque no deja huella.
        bot = cliente.post(
            "/api/v1/visitas/registrar", headers={"User-Agent": UA_BOT}
        )
        assert bot.status_code == 200
        assert bot.json()["contabilizada"] is False


def test_http_catalogo_invalido_falla_cerrado(
    settings: Settings, monkeypatch, mongo_db
) -> None:
    from app.services import visitas_service as modulo_servicio
    from app.services.catalogo_ids import CatalogoInvalido

    def _sin_catalogo():
        raise CatalogoInvalido("sin catálogo")

    monkeypatch.setattr(modulo_servicio, "catalogo_ids", _sin_catalogo)
    app = _app_visitas(settings)
    with TestClient(app) as cliente:
        respuesta = cliente.post(
            "/api/v1/visitas/proyecto",
            json={"proyecto_id": "atlas-vial"},
            headers={"User-Agent": UA_HUMANO},
        )
        assert respuesta.status_code == 503
