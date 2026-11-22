"""Evalúa NL-to-SQL en tres modos: parser, planificador LLM y LLM con fallback.

Uso normal (sin coste, proveedor simulado):

    python scripts/evaluar_nl_sql.py

Llamadas reales (genera cargos; no se ejecuta en las pruebas):

    PORTFOLIO_EVAL_LIVE=1 python scripts/evaluar_nl_sql.py --live

El conjunto es sintético (`app/evals/nl_sql/casos.jsonl`); el esperado se deriva
del contrato del sistema, no de respuestas del modelo.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app.config import Settings  # noqa: E402
from app.evals import nl_sql as evals  # noqa: E402
from app.llm import cliente  # noqa: E402
from app.nl_sql import service as nl_service  # noqa: E402

MODOS = ("parser", "llm", "llm_con_fallback")
GDP = "NY.GDP.MKTP.KD.ZG"
CPI = "FP.CPI.TOTL.ZG"
UEM = "SL.UEM.TOTL.ZS"

#: Perturbaciones deterministas del proveedor simulado. No representan al modelo
#: real: solo permiten medir que el arnés detecta los errores del camino LLM.
PERTURBACIONES: dict[str, dict[str, Any]] = {
    "d03": {"start_year": 2018},          # año fuera de rango
    "d09": {"operation": "min"},           # operación cambiada
    "p02": {"indicator_codes": [UEM]},     # indicador equivocado
    "p09": {"end_year": 2024},             # periodo recortado
    "d15": {"start_year": 2021},           # inicio desplazado
    "p12": {"operation": "trend"},         # pierde el ranking
}
#: El simulado devuelve contenido inválido o fuera de catálogo en estos casos.
INVALIDOS = {"a02", "a05", "n07"}
#: El simulado «alucina» un plan soportado donde el contrato dice que no.
FALSOS_SOPORTADOS = {"n03": {"indicator_codes": [CPI], "start_year": 2019, "end_year": 2025, "operation": "trend"}}


class ProveedorSimulado:
    """Doble del proveedor: respuestas deterministas, sin red.

    Cuenta llamadas y tokens simulados. Documenta en el resultado que sus cifras
    no son del modelo real.
    """

    nombre = "mock-determinista-v1"

    def __init__(self) -> None:
        self.llamadas = 0
        self.tokens = 0
        self.invalidos_emitidos: set[str] = set()

    def respuesta(self, caso: dict[str, Any]) -> dict[str, Any]:
        self.llamadas += 1
        self.tokens += 540
        identificador = caso["id"]
        esperado = caso["esperado"]
        if identificador in INVALIDOS:
            self.invalidos_emitidos.add(identificador)
            contenido = "DELETE FROM observations; no es JSON"
        elif identificador in FALSOS_SOPORTADOS:
            contenido = json.dumps(FALSOS_SOPORTADOS[identificador], ensure_ascii=False)
        elif esperado["soporte"] == "no_soportado":
            contenido = json.dumps({"unsupported": True})
        else:
            plan = {
                "country_code": "MEX",
                "indicator_codes": list(esperado["indicadores"]),
                "start_year": esperado["anio_inicio"],
                "end_year": esperado["anio_fin"],
                "operation": esperado["operacion"],
            }
            plan.update(PERTURBACIONES.get(identificador, {}))
            if plan["operation"] in {"max", "min"} and len(plan["indicator_codes"]) != 1:
                plan["indicator_codes"] = plan["indicator_codes"][:1]
            contenido = json.dumps(plan, ensure_ascii=False)
        return {
            "choices": [{"message": {"role": "assistant", "content": contenido}}],
            "usage": {"prompt_tokens": 420, "completion_tokens": 120, "total_tokens": 540},
        }


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        # Configuracion ficticia: activa el camino LLM sin ninguna clave real.
        llm_base_url="https://proveedor-simulado.invalid/v1",
        llm_api_key="clave-ficticia-de-evaluacion",
        llm_model="mock-determinista-v1",
        nl_sql_audit_enabled=False,
        llm_consultas_por_minuto=120,
        llm_max_solicitudes_por_ip_dia=10_000,
        llm_max_solicitudes_dia=10_000,
        llm_max_tokens_dia=10_000_000,
    )


def evaluar_modo(casos: list[dict[str, Any]], modo: str, servicio: nl_service.CopilotoNlSql) -> list[dict[str, Any]]:
    resultados: list[dict[str, Any]] = []
    for caso in casos:
        inicio = time.perf_counter()
        plan: dict[str, Any] | None = None
        origen = modo
        try:
            if modo == "parser":
                plan = servicio.parse_local(caso["pregunta"])
            elif modo == "llm":
                plan = servicio._plan_llm(caso["pregunta"])
            else:
                plan, origen = servicio.resolver(caso["pregunta"])
        except nl_service.QuestionError:
            plan = None
        latencia = (time.perf_counter() - inicio) * 1000
        comparacion = evals.comparar(plan, caso["esperado"])
        resultados.append(
            {
                "id": caso["id"],
                "categoria": caso["categoria"],
                "esperado_soporte": caso["esperado"]["soporte"],
                "obtenido_soporte": "soportado" if plan is not None else "no_soportado",
                "origen": origen,
                "latencia_ms": round(latencia, 3),
                **comparacion,
            }
        )
    return resultados


def ejecutar(*, live: bool, salida: Path | None) -> dict[str, Any]:
    casos = evals.cargar_casos()
    settings = _settings()
    servicio = nl_service.CopilotoNlSql(None, settings)
    servicio._auditar = lambda *args, **kwargs: None  # sin Mongo en la evaluación
    simulado = ProveedorSimulado()
    original = cliente.post_chat
    tokens_reales: list[int] = []
    modelo = simulado.nombre

    if live:
        if not cliente.configurado(settings):
            raise SystemExit(
                "El proveedor real no está configurado en este entorno; usa el modo simulado."
            )
        modelo = str(settings.llm_model)

        def con_uso(settings_, payload, *, timeout=None):
            sobre = original(settings_, payload, timeout=timeout)
            uso = sobre.get("usage") if isinstance(sobre, dict) else None
            if isinstance(uso, dict) and isinstance(uso.get("total_tokens"), int):
                tokens_reales.append(int(uso["total_tokens"]))
            return sobre

        cliente.post_chat = con_uso
    else:
        def simulado_post(settings_, payload, *, timeout=None):
            identificador = getattr(simulado, "caso_actual", None)
            return simulado.respuesta(identificador)

        cliente.post_chat = simulado_post

    try:
        por_modo: dict[str, Any] = {}
        for modo in MODOS:
            if modo in {"llm", "llm_con_fallback"}:
                for caso in casos:
                    simulado.caso_actual = caso  # el simulado responde por caso
            resultados = []
            for caso in casos:
                simulado.caso_actual = caso
                resultados.extend(evaluar_modo([caso], modo, servicio))
            por_modo[modo] = {
                "metricas": evals.agregar(resultados),
                "por_categoria": evals.por_categoria(resultados),
                "fallidos": [fila["id"] for fila in resultados if not fila["acierto_exacto"]],
            }
    finally:
        cliente.post_chat = original

    tokens = sum(tokens_reales) if live else simulado.tokens
    resultado = {
        "conjunto": {
            "version": evals.VERSION,
            "ruta": "app/evals/nl_sql/casos.jsonl",
            "casos": len(casos),
            "sintetico": True,
        },
        "ejecucion": {
            "fecha": datetime.now(timezone.utc).isoformat(),
            "modo": "live" if live else "simulado",
            "modelo": modelo,
            "proveedor_real": live,
            "llamadas": len(tokens_reales) if live else simulado.llamadas,
        },
        "limites": {
            "pregunta_max_caracteres": evals.PREGUNTA_MAX,
            "max_tokens_por_consulta": int(settings.llm_max_tokens),
            "cliente_compartido": True,
        },
        "metricas_por_modo": por_modo,
        "planes_invalidos_bloqueados": sorted(simulado.invalidos_emitidos)
        if not live
        else "no medido en modo live",
        "tokens_totales": tokens,
        "costo": {
            "usd": "no calculable",
            "motivo": "no hay tabla de precios verificada en el repositorio; no se estima sin base",
        },
        "notas": [
            "Conjunto sintético: preguntas escritas a mano, sin datos privados.",
            "Los esperados se derivan del contrato del sistema (catálogo, años y operaciones), no de respuestas del modelo.",
            "En modo simulado el proveedor es un doble determinista; sus cifras no representan al modelo real.",
            "La latencia del modo simulado mide el arnés, no la red del proveedor.",
            "El costo en USD no es calculable sin tabla de precios; los tokens sí se registran.",
        ],
    }
    destino = salida or (
        evals.RESULTADOS
        / f"nl_sql-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{'live' if live else 'simulado'}-v{evals.VERSION.replace('.', '')}.json"
    )
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    return resultado


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evalúa NL-to-SQL con el conjunto versionado.")
    parser.add_argument("--live", action="store_true", help="usar el proveedor real (genera cargos)")
    parser.add_argument("--salida", type=Path, default=None, help="ruta del JSON de resultados")
    args = parser.parse_args(argv)

    if args.live:
        print(
            "AVISO: --live llama al proveedor real y PUEDE GENERAR CARGOS. "
            "No debe ejecutarse durante las pruebas normales.",
            file=sys.stderr,
        )
        if os.environ.get("PORTFOLIO_EVAL_LIVE") != "1":
            print(
                "Bloqueado: define PORTFOLIO_EVAL_LIVE=1 para confirmar que aceptas los cargos.",
                file=sys.stderr,
            )
            return 2

    resultado = ejecutar(live=args.live, salida=args.salida)
    for modo, datos in resultado["metricas_por_modo"].items():
        metricas = datos["metricas"]
        print(
            f"{modo:18} acierto={metricas['acierto_exacto']} "
            f"indicadores={metricas['precision_indicadores']} "
            f"años={metricas['precision_anios']} "
            f"operación={metricas['precision_operacion']} "
            f"no_soportado={metricas['deteccion_no_soportado']} "
            f"falsos_soportados={metricas['falsos_soportados']}"
        )
    print(f"resultados: {resultado['ejecucion']['modo']} · tokens={resultado['tokens_totales']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
