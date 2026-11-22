"""Cuotas estrictas de las consultas LLM: 1 por IP/día, 10 globales/día.

Sin red y sin MongoDB real: se usa un doble de la colección que imita las
operaciones atómicas y se sustituye el transporte del proveedor.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.db import mongo as mongo_mod
from app.llm import cliente, contexto, limites

CLAVE = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


class ColeccionFalsa:
    """Imita `find_one_and_update`, `find_one` y `create_index` con bloqueo."""

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
    def __init__(self, coleccion: ColeccionFalsa) -> None:
        self.coleccion = coleccion

    def __getitem__(self, nombre):
        return self.coleccion


class ClienteFalsa:
    def __init__(self, base: BaseFalsa) -> None:
        self.base = base

    def __getitem__(self, nombre):
        return self.base


class DbFalsa:
    def __init__(self, base: BaseFalsa) -> None:
        self.client = ClienteFalsa(base)


class MongoFalsa:
    def __init__(self, base: BaseFalsa) -> None:
        self.db = DbFalsa(base)


def ajustes(**extra) -> Settings:
    base = {
        "_env_file": None,
        "llm_base_url": "https://proveedor.invalid/v1",
        "llm_api_key": "clave-ficticia-de-prueba",
        "llm_model": "modelo-de-prueba",
        "llm_cuota_hmac_key": CLAVE,
        "llm_max_tokens": 300,
        "llm_max_solicitudes_por_ip_dia": 1,
        "llm_max_solicitudes_dia": 10,
        "llm_max_tokens_dia": 3_000,
        "llm_intervalo_minimo_s": 600,
        "llm_consultas_por_minuto": 120,
    }
    base.update(extra)
    return Settings(**base)


@pytest.fixture()
def almacen(monkeypatch):
    coleccion = ColeccionFalsa()
    base = BaseFalsa(coleccion)
    monkeypatch.setattr(mongo_mod, "get_database", lambda: MongoFalsa(base))
    cliente.VENTANA.reiniciar()
    return coleccion


def reservar(settings: Settings, ip: str = "203.0.113.9"):
    return limites.reservar_llamada(settings, ip=ip)


# ---------------------------------------------------------------- 1. cuotas
def test_primera_consulta_permitida_y_segunda_bloqueada(almacen) -> None:
    settings = ajustes()
    assert reservar(settings)["visitante_del_dia"] == 1
    with pytest.raises(limites.CuotaExcedida) as excinfo:
        reservar(settings)
    # Dentro del intervalo: mensaje de espera con Retry-After corto.
    assert 1 <= excinfo.value.reintentar_en <= 600
    assert "espera" in str(excinfo.value).lower()


def test_intervalo_minimo_y_limite_diario_se_distinguen(almacen) -> None:
    settings = ajustes()
    reservar(settings)
    clave = limites.identificador("203.0.113.9", limites._dia_utc(), settings)
    documento = almacen.documentos[f"ip:{clave}:{limites._dia_utc()}"]
    documento["ultima"] = int(
        (datetime.now(timezone.utc) - timedelta(minutes=11)).timestamp() * 1000
    )
    with pytest.raises(limites.CuotaExcedida) as excinfo:
        reservar(settings)
    assert "consulta de hoy" in str(excinfo.value)
    assert excinfo.value.reintentar_en > 600  # hasta la medianoche UTC


def test_cuota_global_diaria(almacen) -> None:
    settings = ajustes(llm_max_solicitudes_dia=2, llm_max_tokens_dia=10_000)
    reservar(settings, ip="203.0.113.1")
    reservar(settings, ip="203.0.113.2")
    with pytest.raises(limites.CuotaExcedida, match="portafolio agotó sus consultas"):
        reservar(settings, ip="203.0.113.3")


def test_cuota_de_tokens_global(almacen) -> None:
    settings = ajustes(llm_max_solicitudes_dia=10, llm_max_tokens_dia=1_000)
    reservar(settings, ip="203.0.113.1")  # 300
    reservar(settings, ip="203.0.113.2")  # 600
    reservar(settings, ip="203.0.113.3")  # 900
    with pytest.raises(limites.CuotaExcedida, match="presupuesto diario de tokens"):
        reservar(settings, ip="203.0.113.4")


def test_sin_clave_hmac_falla_cerrado(almacen, monkeypatch) -> None:
    settings = ajustes(llm_cuota_hmac_key=None)
    with pytest.raises(limites.LimitesNoConfigurados, match="LLM_CUOTA_HMAC_KEY"):
        reservar(settings)

    # Y el transporte no llega al proveedor.
    llamado = {"si": False}

    def urlopen_prohibido(*args, **kwargs):
        llamado["si"] = True
        raise AssertionError("no debe llamarse al proveedor sin clave de cuotas")

    monkeypatch.setattr(cliente, "urlopen", urlopen_prohibido)
    with pytest.raises(limites.LimitesNoConfigurados):
        cliente.post_chat(settings, {"model": "m"})
    assert llamado["si"] is False


def test_solicitudes_concurrentes_no_rebasan_el_cupo(almacen) -> None:
    settings = ajustes()
    resultados: list[str] = []
    candado = threading.Lock()

    def intento() -> None:
        try:
            reservar(settings)
            with candado:
                resultados.append("ok")
        except limites.CuotaExcedida:
            with candado:
                resultados.append("429")

    hilos = [threading.Thread(target=intento) for _ in range(8)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()
    assert resultados.count("ok") == 1
    assert resultados.count("429") == 7


def test_reinicio_y_multiples_workers_comparten_contadores(almacen) -> None:
    settings = ajustes()
    base = BaseFalsa(almacen)
    primero = limites.limitador_para(base, settings)
    segundo = limites.limitador_para(base, settings)  # otro worker / reinicio
    primero.reservar(ip="203.0.113.9")
    with pytest.raises(limites.CuotaExcedida):
        segundo.reservar(ip="203.0.113.9")


# ------------------------------------------------------------------- 2. IP
def test_ip_falsificada_se_ignora(almacen) -> None:
    settings = ajustes()
    cabeceras = {"x-real-ip": "198.51.100.66", "x-forwarded-for": "198.51.100.66"}
    # Conexión directa (no es proxy confiable): manda la IP de la conexión.
    assert limites.ip_real("203.0.113.9", cabeceras, settings) == "203.0.113.9"
    # Proxy confiable: se acepta la cabecera.
    assert limites.ip_real("172.18.0.5", cabeceras, settings) == "198.51.100.66"
    # Y la cuota se cuenta sobre la IP efectiva, no sobre la falsificada.
    assert limites.identificador("203.0.113.9", "2026-11-22", settings) != limites.identificador(
        "198.51.100.66", "2026-11-22", settings
    )


def test_identificador_no_expone_la_ip(almacen) -> None:
    settings = ajustes()
    valor = limites.identificador("203.0.113.9", "2026-11-22", settings)
    assert re.fullmatch(r"[0-9a-f]{32}", valor)
    assert "203.0.113.9" not in valor
    assert valor != limites.identificador("203.0.113.9", "2026-11-23", settings)
    assert json.dumps(almacen.documentos) .find("203.0.113.9") == -1


# --------------------------------------------------- 3. transporte y presupuesto
def test_tope_de_salida_300_aunque_el_llamador_pida_mas(almacen, monkeypatch) -> None:
    capturado = {}

    class Respuesta:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

    def falso(peticion, timeout=None):
        capturado["payload"] = json.loads(peticion.data.decode())
        return Respuesta()

    monkeypatch.setattr(cliente, "urlopen", falso)
    cliente.post_chat(ajustes(), {"model": "m", "max_tokens": 700})
    assert capturado["payload"]["max_tokens"] == 300


def test_una_llamada_que_falla_consume_cupo(almacen, monkeypatch) -> None:
    settings = ajustes()

    def falla(peticion, timeout=None):
        raise TimeoutError("sin respuesta")

    monkeypatch.setattr(cliente, "urlopen", falla)
    with pytest.raises(cliente.LlmUpstreamError):
        cliente.post_chat(settings, {"model": "m"})
    # La reserva ya se hizo: el fallo no regala una segunda consulta.
    with pytest.raises(limites.CuotaExcedida):
        cliente.post_chat(settings, {"model": "m"})


def test_el_contexto_de_ip_alimenta_la_cuota(almacen, monkeypatch) -> None:
    settings = ajustes()
    token = contexto.fijar_ip("198.51.100.7")
    try:
        limites.reservar_llamada(settings, ip=contexto.ip_actual())
        clave = limites.identificador("198.51.100.7", limites._dia_utc(), settings)
        assert f"ip:{clave}:{limites._dia_utc()}" in almacen.documentos
    finally:
        contexto.restablecer(token)


# --------------------------------------- 4. cobertura de las seis demos y NL-to-SQL
def test_un_solo_transporte_en_la_api() -> None:
    """El endpoint del proveedor se construye en un único módulo."""
    import pathlib

    raiz = pathlib.Path(__file__).resolve().parents[1] / "app"
    constructores = [
        archivo.relative_to(raiz).as_posix()
        for archivo in raiz.rglob("*.py")
        if "chat/completions" in archivo.read_text(encoding="utf-8")
    ]
    assert constructores == ["llm/cliente.py"], constructores
    llamadores = [
        archivo.relative_to(raiz).as_posix()
        for archivo in raiz.rglob("*.py")
        if "from app.llm import" in archivo.read_text(encoding="utf-8")
    ]
    assert len(llamadores) >= 6, llamadores


def test_nl_to_sql_usa_el_cliente_y_su_limite_de_pregunta() -> None:
    import pathlib

    raiz = pathlib.Path(__file__).resolve().parents[1] / "app"
    servicio = (raiz / "nl_sql" / "service.py").read_text(encoding="utf-8")
    ruta = (raiz / "api" / "v1" / "routes" / "copiloto_nl_sql.py").read_text(encoding="utf-8")
    assert "from app.llm import cliente" in servicio
    assert "cliente.solicitar_json(" in servicio
    assert "urlopen" not in servicio and "Request(" not in servicio
    assert "max_length=500" in ruta  # límite de longitud de la pregunta en la ruta


def test_las_seis_demos_pasan_por_el_cliente(monkeypatch, almacen) -> None:
    """Cada flujo llama al menos una vez al transporte común."""
    llamadas = {"n": 0}

    def falso_post_chat(settings, payload, *, timeout=None):
        llamadas["n"] += 1
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(cliente, "post_chat", falso_post_chat)

    # Los tres análisis consultivos.
    from app.analisis import dockmind, kilometro_cero, ripple_room

    with pytest.raises(cliente.LlmRespuestaInvalida):
        kilometro_cero.analizar(
            ajustes(),
            {
                "escenario": {"id": "s", "nombre": "S", "descripcion": ""},
                "red": {"envios": 1, "a_tiempo": 1, "retrasados": 0, "en_riesgo": 0, "corredores_activos": 1},
                "corredores": [
                    {"id": "C-1", "origen": "A", "destino": "B", "envios": 1, "a_tiempo": 1, "retrasados": 0, "en_riesgo": 0, "km": 10}
                ],
                "incidencias": [],
                "acciones_disponibles": list(kilometro_cero.ACCIONES),
            },
        )
    with pytest.raises(cliente.LlmRespuestaInvalida):
        ripple_room.analizar(
            ajustes(),
            {
                "escenario": {"id": "s", "nombre": "S", "descripcion": ""},
                "resultado": {"service": 90, "shortage": 0, "backlog": 0, "cost": 0},
                "roles": [{"id": "ops", "nombre": "Operaciones", "interes": ""}],
                "plan": {"id": "p", "nombre": "P"},
            },
        )
    with pytest.raises(cliente.LlmRespuestaInvalida):
        dockmind.analizar(
            ajustes(),
            {
                "escenario": {"id": "s", "nombre": "S", "parametros": {}},
                "sku": {"sku": "1", "nombre": "N", "categoria": "", "lead": 1, "pack": 1, "stock": 1, "precio": 1, "costo": 1},
                "pronostico": {"metodo": "m", "wape": 0.1, "mae": 1, "horizonte": 1},
                "plan_compra": [],
                "restricciones": {"presupuesto": 1, "buffer_dias": 0},
                "metricas": {"valores": {}},
            },
        )
    # Data Incident Commander y el briefing de Banxico.
    from app.incident_commander.service import IncidentCommander
    from app.rag_banxico.service import BriefingBanxico

    class FakeDb:
        ping = False
        client = None

    from app.incident_commander import service as dic_service

    comandante = IncidentCommander(ajustes())
    run = comandante.crear_run("missing")
    with pytest.raises(dic_service.LlmRespuestaInvalida):
        comandante.analizar(run["run_id"])
    briefing = BriefingBanxico(FakeDb(), ajustes())
    evidencia = briefing.recuperar("¿Cuál fue la decisión sobre la tasa?")[0]
    respuesta, modo = briefing.responder("¿Cuál fue la decisión sobre la tasa?", evidencia)
    assert modo in {"llm-grounded", "extractiva-respaldo"}

    # NL-to-SQL.
    from app.nl_sql.service import CopilotoNlSql

    copiloto = CopilotoNlSql(None, ajustes())
    copiloto._auditar = lambda *a, **k: None
    copiloto.consultar("¿Cómo evolucionó la inflación?")

    assert llamadas["n"] >= 5
