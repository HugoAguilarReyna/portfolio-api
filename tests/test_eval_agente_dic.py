"""Pruebas del evaluador del agente: proveedor, métricas dinámicas y salidas."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.db import mongo as mongo_mod
from app.evals import dic as evals
from app.llm import cliente

RAIZ = Path(__file__).resolve().parents[1]
SCRIPT = RAIZ / "scripts" / "evaluar_agente_dic.py"


class ColeccionFalsa:
    """Almacén de cuotas en memoria (la lógica de cuotas es la real)."""

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


class _Base:
    def __init__(self, coleccion):
        self.coleccion = coleccion

    def __getitem__(self, nombre):
        return self.coleccion


class _Cliente:
    def __init__(self, coleccion):
        self.base = _Base(coleccion)

    def __getitem__(self, nombre):
        return self.base


class _Db:
    def __init__(self, coleccion):
        self.client = _Cliente(coleccion)


class _Mongo:
    def __init__(self, coleccion):
        self.db = _Db(coleccion)


@pytest.fixture()
def almacen(monkeypatch):
    coleccion = ColeccionFalsa()
    monkeypatch.setattr(mongo_mod, "get_database", lambda: _Mongo(coleccion))
    return coleccion


def limites_de_evaluacion(monkeypatch) -> None:
    """Cupos propios de la evaluación real (no los de visitantes).

    Doce incidentes reservan una vez cada uno: con el cupo de producción
    (23 000 tokens/día) la corrida se cortaría a la mitad. Se usa un cupo de
    evaluación amplio y un presupuesto por run pequeño, sin tocar los valores
    de producción que valida `test_coherencia_limites`.
    """
    monkeypatch.setenv("LLM_MAX_SOLICITUDES_POR_IP_DIA", "50")
    monkeypatch.setenv("LLM_MAX_SOLICITUDES_DIA", "50")
    monkeypatch.setenv("LLM_MAX_TOKENS_DIA", "1000000")
    monkeypatch.setenv("LLM_AGENTE_PRESUPUESTO_TOKENS", "3000")
    monkeypatch.setenv("LLM_INTERVALO_MINIMO_S", "0")


def cargar_script():
    especificacion = importlib.util.spec_from_file_location("evaluar_agente_dic", SCRIPT)
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    return modulo


def plan_fijo(uso: dict | None = None) -> dict:
    """Respuesta cerrada del modelo (una sola vuelta, sin herramientas)."""
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "recommendation": "quarantine",
                            "justification": "Plan de prueba.",
                            "evidence": ["Evidencia de prueba."],
                            "risks": ["Riesgo de prueba."],
                        }
                    )
                }
            }
        ],
        "usage": uso or {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


def env_real(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "clave-ficticia-de-prueba")
    monkeypatch.setenv("LLM_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("LLM_CUOTA_HMAC_KEY", "0123456789abcdef0123456789abcdef")


def test_metricas_dinamicas_salen_de_las_filas() -> None:
    modulo = cargar_script()
    filas = [
        {"ok": True, "plan_correcto": True, "herramientas_ok": True, "precision": 1.0, "pasos": 2, "escrituras": 0},
        {"ok": True, "plan_correcto": True, "herramientas_ok": False, "precision": 0.5, "pasos": 3, "escrituras": 0},
        {"ok": True, "plan_correcto": False, "herramientas_ok": True, "precision": 0.7, "pasos": 4, "escrituras": 0},
        {"ok": False, "bloqueado": "AgentePolitica"},
    ]
    metricas = modulo.calcular_metricas(filas)
    assert metricas["total"] == 4
    assert metricas["correctos"] == 1  # solo la primera cumple plan + herramientas
    assert metricas["incorrectos"] == 3
    assert metricas["tasa_acierto"] == 0.25
    assert metricas["violaciones_de_politica"] == 1
    # Sin constantes: otra entrada cambia las métricas.
    otra = modulo.calcular_metricas(filas[:2])
    assert (otra["total"], otra["correctos"], otra["tasa_acierto"]) == (2, 1, 0.5)


def test_baseline_se_calcula_con_el_conjunto() -> None:
    modulo = cargar_script()
    casos = evals.cargar_incidentes()
    baseline = modulo.calcular_baseline(casos)
    evaluables = [c for c in casos if not c.get("violacion")]
    aciertos = [c for c in evaluables if c["plan"] == "quarantine"]
    assert baseline["incidentes_evaluables"] == len(evaluables)
    assert baseline["correctos"] == len(aciertos)
    assert baseline["incorrectos"] == len(evaluables) - len(aciertos)
    assert baseline["excluidos_por_violacion"] == [c["id"] for c in casos if c.get("violacion")]


def test_rutas_por_proveedor_no_colisionan() -> None:
    modulo = cargar_script()
    rutas = {proveedor: modulo.ruta_salida(proveedor) for proveedor in modulo.PROVEEDORES}
    assert len(set(rutas.values())) == len(rutas)
    for proveedor, ruta in rutas.items():
        assert ruta.name == f"evaluacion_agente_dic_{proveedor}.json"
        # No reutiliza el archivo simulado de referencia.
        assert ruta.name != "agente-2026-11-22-simulado.json"


def test_no_sobrescribe_sin_forzar(tmp_path) -> None:
    modulo = cargar_script()
    destino = tmp_path / "resultado.json"
    destino.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="ya existe"):
        modulo.verificar_destino(destino)
    modulo.verificar_destino(destino, forzar=True)  # no lanza


def test_simulado_por_defecto_escribe_metricas_coherentes(tmp_path) -> None:
    modulo = cargar_script()
    salida = tmp_path / "simulado.json"
    codigo = modulo.main(["--salida", str(salida)])
    assert codigo == 0
    datos = json.loads(salida.read_text(encoding="utf-8"))
    casos = evals.cargar_incidentes()
    assert datos["proveedor"] == "simulado"
    assert datos["ejecucion"]["modo"] == "simulado"
    assert datos["metricas"]["total"] == len(casos)
    assert (
        datos["metricas"]["correctos"] + datos["metricas"]["incorrectos"]
        == datos["metricas"]["total"]
    )
    calculated = sum(
        1
        for fila in datos["por_incidente"]
        if fila.get("ok") and fila.get("plan_correcto") and fila.get("herramientas_ok")
    )
    assert datos["metricas"]["correctos"] == calculated
    assert datos["metricas"]["tasa_acierto"] == round(calculated / len(casos), 4)


@pytest.mark.parametrize("proveedor", ["openai", "deepseek"])
def test_proveedor_real_sin_credenciales_falla_antes_de_empezar(
    tmp_path, monkeypatch, proveedor
) -> None:
    modulo = cargar_script()
    for variable in ("LLM_API_KEY", "LLM_MODEL", "LLM_CUOTA_HMAC_KEY"):
        monkeypatch.delenv(variable, raising=False)
    salida = tmp_path / f"{proveedor}.json"
    with pytest.raises(SystemExit, match="LLM_API_KEY"):
        modulo.main(["--proveedor", proveedor, "--salida", str(salida)])
    assert not salida.exists()


def test_proveedor_real_doblado_sin_red(tmp_path, monkeypatch, almacen) -> None:
    """Con credenciales ficticias y transporte doblado: no hay red real."""
    modulo = cargar_script()
    env_real(monkeypatch)
    limites_de_evaluacion(monkeypatch)
    casos = evals.cargar_incidentes()
    original = cliente.post_chat
    cliente.post_chat = lambda settings, payload, *, timeout=None: plan_fijo()
    try:
        salida = tmp_path / "real.json"
        codigo = modulo.main(["--proveedor", "openai", "--salida", str(salida)])
    finally:
        cliente.post_chat = original
    assert codigo == 0
    datos = json.loads(salida.read_text(encoding="utf-8"))
    assert datos["proveedor"] == "openai"
    assert datos["ejecucion"]["proveedor_real"] is True
    assert datos["ejecucion"]["modelo"] == "gpt-4.1-mini"
    assert datos["metricas"]["total"] == len(casos)
    assert datos["metricas"]["total"] == (
        datos["metricas"]["correctos"] + datos["metricas"]["incorrectos"]
    )


def test_aborta_si_el_costo_estimado_supera_el_tope(tmp_path, monkeypatch, almacen) -> None:
    modulo = cargar_script()
    env_real(monkeypatch)
    limites_de_evaluacion(monkeypatch)
    casos = evals.cargar_incidentes()
    original = cliente.post_chat
    caro = {"prompt_tokens": 5_000_000, "completion_tokens": 1_000_000}
    cliente.post_chat = lambda settings, payload, *, timeout=None: plan_fijo(caro)
    try:
        salida = tmp_path / "caro.json"
        codigo = modulo.main(
            ["--proveedor", "openai", "--salida", str(salida), "--max-costo-usd", "0.000001"]
        )
    finally:
        cliente.post_chat = original

    assert codigo == 2
    datos = json.loads(salida.read_text(encoding="utf-8"))
    assert datos["ejecucion"]["abortada_por_costo"] is True
    assert len(datos["por_incidente"]) == 1  # se cortó tras el primer incidente
    assert len(datos["por_incidente"]) < len(casos)


def test_costo_estimado_con_tarifa_verificada() -> None:
    modulo = cargar_script()
    assert modulo.costo_estimado("gpt-4.1-mini", 1_000_000, 1_000_000) == 2.0
    assert modulo.costo_estimado("modelo-sin-tarifa", 1_000, 1_000) is None
