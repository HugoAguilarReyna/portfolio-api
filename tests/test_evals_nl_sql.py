"""Pruebas deterministas del conjunto y del arnés de evaluación de NL-to-SQL."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.evals import nl_sql as evals
from app.nl_sql.service import CopilotoNlSql

RAIZ = Path(__file__).resolve().parents[1]
SCRIPT = RAIZ / "scripts" / "evaluar_nl_sql.py"


def cargar_script():
    especificacion = importlib.util.spec_from_file_location("evaluar_nl_sql", SCRIPT)
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    return modulo


def test_conjunto_cumple_el_contrato() -> None:
    casos = evals.cargar_casos()
    assert 40 <= len(casos) <= 60
    assert {fila["categoria"] for fila in casos} == set(evals.CATEGORIAS)
    for fila in casos:
        assert evals.PREGUNTA_MIN <= len(fila["pregunta"]) <= evals.PREGUNTA_MAX
        if fila["esperado"]["soporte"] == "soportado":
            assert set(fila["esperado"]["indicadores"]) <= {
                "NY.GDP.MKTP.KD.ZG",
                "FP.CPI.TOTL.ZG",
                "SL.UEM.TOTL.ZS",
            }
            assert 2019 <= fila["esperado"]["anio_inicio"] <= fila["esperado"]["anio_fin"] <= 2025


def test_conjunto_no_contiene_secretos_ni_datos_privados() -> None:
    crudo = evals.CASOS.read_text(encoding="utf-8")
    for patron in ("sk-", "github_pat_", "Bearer ", "password", "api_key=", "@gmail", "@hotmail"):
        assert patron not in crudo, patron


def test_parser_respeta_soportados_y_rechaza_los_fuera_de_contrato() -> None:
    """El conjunto mide el contrato: los desvíos del parser quedan registrados."""
    servicio = CopilotoNlSql(None, Settings(_env_file=None, nl_sql_audit_enabled=False))
    fallidos: list[str] = []
    for caso in evals.cargar_casos():
        try:
            plan = servicio.parse_local(caso["pregunta"])
        except Exception:
            plan = None
        if caso["esperado"]["soporte"] == "soportado":
            assert evals.comparar(plan, caso["esperado"])["acierto_exacto"] is True, caso["id"]
        elif not evals.comparar(plan, caso["esperado"])["soporte_ok"]:
            fallidos.append(caso["id"])
    # n12/n13/a09/a10 quedan como regresiones de errores previamente publicados.
    assert fallidos == []


def test_operacion_y_plan_normalizados() -> None:
    assert evals.operacion_de({"kind": "ranking", "rank_order": "DESC"}) == "max"
    assert evals.operacion_de({"kind": "ranking", "rank_order": "ASC"}) == "min"
    assert evals.operacion_de({"kind": "comparison"}) == "comparison"
    assert evals.operacion_de({"kind": "otro"}) is None
    assert evals.plan_de(None) is None
    plan = evals.plan_de(
        {"kind": "trend", "indicator_codes": ["B", "A"], "start_year": 2020, "end_year": 2021}
    )
    assert plan == {
        "indicadores": ["A", "B"],
        "anio_inicio": 2020,
        "anio_fin": 2021,
        "operacion": "trend",
    }


def test_metricas_agregadas() -> None:
    resultados = [
        {"esperado_soporte": "soportado", "acierto_exacto": True, "indicadores_ok": True,
         "anios_ok": True, "operacion_ok": True, "soporte_ok": True, "latencia_ms": 1.0,
         "tokens": 10, "categoria": "directa"},
        {"esperado_soporte": "soportado", "acierto_exacto": False, "indicadores_ok": True,
         "anios_ok": False, "operacion_ok": True, "soporte_ok": True, "latencia_ms": 2.0,
         "tokens": 10, "categoria": "directa"},
        {"esperado_soporte": "no_soportado", "acierto_exacto": True, "indicadores_ok": None,
         "anios_ok": None, "operacion_ok": None, "soporte_ok": True, "latencia_ms": 3.0,
         "tokens": 10, "categoria": "adversarial"},
        {"esperado_soporte": "no_soportado", "acierto_exacto": False, "indicadores_ok": False,
         "anios_ok": False, "operacion_ok": False, "soporte_ok": False, "latencia_ms": 4.0,
         "tokens": 10, "categoria": "adversarial"},
    ]
    metricas = evals.agregar(resultados)
    assert metricas["acierto_exacto"] == 0.5
    assert metricas["precision_indicadores"] == 1.0
    assert metricas["precision_anios"] == 0.5
    assert metricas["deteccion_no_soportado"] == 0.5
    assert metricas["falsos_soportados"] == 1
    assert metricas["tokens_totales"] == 40


def test_el_arnes_es_determinista_en_modo_simulado(tmp_path) -> None:
    modulo = cargar_script()
    salida = tmp_path / "resultado.json"
    resultado = modulo.ejecutar(live=False, salida=salida)

    assert resultado["ejecucion"]["proveedor_real"] is False
    assert resultado["conjunto"]["casos"] == 51
    parser = resultado["metricas_por_modo"]["parser"]["metricas"]
    llm = resultado["metricas_por_modo"]["llm"]["metricas"]
    combo = resultado["metricas_por_modo"]["llm_con_fallback"]["metricas"]

    assert parser["acierto_exacto"] == 1.0
    assert parser["falsos_soportados"] == 0
    assert llm["acierto_exacto"] == 0.7857
    assert llm["falsos_soportados"] == 0
    assert combo["acierto_exacto"] == 0.8214
    assert resultado["planes_invalidos_bloqueados"] == ["a02", "a05", "n07"]
    assert resultado["costo"]["usd"] == "no calculable"
    assert resultado["tokens_totales"] > 0
    assert json.loads(salida.read_text(encoding="utf-8"))["conjunto"]["version"] == evals.VERSION


def test_el_modo_live_exige_confirmacion_explicita(monkeypatch) -> None:
    modulo = cargar_script()
    monkeypatch.delenv("PORTFOLIO_EVAL_LIVE", raising=False)
    assert modulo.main(["--live"]) == 2
    # El modo simulado no requiere confirmación.
    assert modulo.main(["--salida", str(Path("resultado-temporal.json"))]) == 0
    Path("resultado-temporal.json").unlink(missing_ok=True)
