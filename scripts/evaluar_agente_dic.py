"""Evalúa el agente con herramientas sobre incidentes sintéticos.

    python scripts/evaluar_agente_dic.py                      # proveedor simulado
    python scripts/evaluar_agente_dic.py --proveedor openai    # requiere LLM_API_KEY

El conjunto es sintético (`app/evals/dic/incidentes.jsonl`). Cada corrida escribe
SU archivo (`evaluacion_agente_dic_<proveedor>.json`) y no sobrescribe resultados
previos salvo que se pase `--forzar`.

Qué mide cada modo:
* `simulado`  → contrato y fontanería del agente (guardas, herramientas, límites).
* real (`openai`, `deepseek`) → comportamiento del modelo, tokens y costo medidos.

No se ejecuta ninguna llamada real en las pruebas; los proveedores reales exigen
credenciales en el entorno y abortan antes de empezar si faltan.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app.config import Settings  # noqa: E402
from app.evals import dic as evals  # noqa: E402
from app.incident_commander import agente  # noqa: E402
from app.incident_commander.service import IncidentCommander  # noqa: E402
from app.llm import cliente, contexto, limites  # noqa: E402

PROVEEDORES = ("simulado", "openai", "deepseek")
BASES = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
}
#: USD por millón de tokens (entrada, salida). Verificado el 2026-11-22 en
#: https://developers.openai.com/api/docs/pricing (Standard, gpt-4.1-mini).
TARIFAS = {"gpt-4.1-mini": (0.40, 1.60)}
IDENTIFICADOR_EVALUACION = "evaluacion-automatica"


class ProveedorGuiado:
    """Doble determinista: devuelve la secuencia guionizada de cada incidente."""

    def __init__(self, incidentes: list[dict[str, Any]]) -> None:
        self.plan = {fila["id"]: fila for fila in incidentes}
        self.actual: dict[str, Any] | None = None
        self.paso = 0

    def __call__(self, settings, payload, *, timeout=None):
        caso = self.actual
        assert caso is not None
        self.paso += 1
        secuencia = caso["herramientas"]
        if self.paso <= len(secuencia):
            nombre = secuencia[self.paso - 1]
            argumentos = caso.get("argumentos", {}).get(nombre, {})
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": f"call-{self.paso}",
                                    "type": "function",
                                    "function": {"name": nombre, "arguments": json.dumps(argumentos)},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"total_tokens": 120},
            }
        contenido = caso.get("salida") or json.dumps(
            {
                "recommendation": caso["plan"],
                "justification": caso["justificacion"],
                "evidence": [
                    item
                    if ":" in item.split(" ")[0]
                    else f"{caso['herramientas'][0]}: {item}"
                    for item in caso["evidencia"]
                ],
                "risks": caso["riesgos"],
            },
            ensure_ascii=False,
        )
        return {
            "choices": [{"message": {"role": "assistant", "content": contenido}}],
            "usage": {"total_tokens": 120},
        }


class MedidorTokens:
    """Envuelve el transporte real para acumular tokens y estimar el costo."""

    def __init__(self, original) -> None:
        self.original = original
        self.entrada = 0
        self.salida = 0

    def __call__(self, settings, payload, *, timeout=None):
        sobre = self.original(settings, payload, timeout=timeout)
        uso = sobre.get("usage") if isinstance(sobre, dict) else None
        if isinstance(uso, dict):
            if isinstance(uso.get("prompt_tokens"), int):
                self.entrada += int(uso["prompt_tokens"])
            if isinstance(uso.get("completion_tokens"), int):
                self.salida += int(uso["completion_tokens"])
        return sobre


def ruta_salida(proveedor: str, base: Path | None = None) -> Path:
    """Un archivo por proveedor: nunca reutiliza el nombre de otra corrida."""
    return (base or evals.RESULTADOS) / f"evaluacion_agente_dic_{proveedor}.json"


def verificar_destino(destino: Path, *, forzar: bool = False) -> None:
    """No se sobrescribe un resultado previo sin pedirlo explícitamente."""
    if destino.exists() and not forzar:
        raise SystemExit(
            f"El destino ya existe: {destino}. Usa --salida para otro archivo o "
            "--forzar si quieres reemplazarlo."
        )


def costo_estimado(modelo: str, tokens_entrada: int, tokens_salida: int) -> float | None:
    """Costo en USD con la tarifa verificada; None si el modelo no está tarifado."""
    tarifa = TARIFAS.get(modelo)
    if not tarifa:
        return None
    entrada, salida = tarifa
    return round(tokens_entrada * entrada / 1e6 + tokens_salida * salida / 1e6, 6)


def costo_cota_superior(modelo: str, presupuesto_tokens: int) -> float | None:
    """Cota conservadora por incidente: todo el presupuesto a la tarifa de salida."""
    tarifa = TARIFAS.get(modelo)
    if not tarifa:
        return None
    return round(int(presupuesto_tokens) * max(tarifa[0], tarifa[1]) / 1e6, 6)


def settings_simulado() -> Settings:
    return Settings(
        _env_file=None,
        llm_base_url="https://proveedor-simulado.invalid/v1",
        llm_api_key="clave-ficticia-de-evaluacion",
        llm_model="mock-agente-v1",
        llm_consultas_por_minuto=120,
        llm_agente_max_pasos=6,
        llm_agente_presupuesto_tokens=5_000,
    )


def settings_real(proveedor: str) -> Settings:
    """Proveedor real: credenciales SOLO del entorno; nunca del repositorio."""
    faltantes = []
    if not (os.environ.get("LLM_API_KEY") or "").strip():
        faltantes.append("LLM_API_KEY")
    if not (os.environ.get("LLM_MODEL") or "").strip():
        faltantes.append("LLM_MODEL")
    if not (os.environ.get("LLM_CUOTA_HMAC_KEY") or "").strip():
        faltantes.append("LLM_CUOTA_HMAC_KEY")
    if faltantes:
        raise SystemExit(
            f"El proveedor «{proveedor}» requiere en el entorno: {', '.join(faltantes)}. "
            "No se inició la corrida (no se hizo ninguna llamada)."
        )
    ajustes = Settings(_env_file=None)
    if not (ajustes.llm_base_url or "").strip():
        ajustes = ajustes.model_copy(update={"llm_base_url": BASES[proveedor]})
    return ajustes


def calcular_metricas(filas: list[dict[str, Any]]) -> dict[str, Any]:
    """Métricas separadas de selección, plan, bloqueos, llamadas, latencia y costo."""
    validos = [fila for fila in filas if not fila.get("adversarial")]
    cerrados = [fila for fila in validos if fila.get("ok")]
    correctos_plan = [fila for fila in cerrados if fila.get("plan_correcto")]
    herramientas_exactas = [fila for fila in cerrados if fila.get("herramientas_ok")]
    precisiones = [fila["precision"] for fila in cerrados if fila.get("precision") is not None]
    recalls = [fila["recall"] for fila in cerrados if fila.get("recall") is not None]
    latencias = [fila["latencia_ms"] for fila in filas if fila.get("latencia_ms") is not None]
    adversariales = [fila for fila in filas if fila.get("adversarial")]
    bloqueos = [
        fila for fila in adversariales if fila.get("bloqueado") in {"AgentePolitica"}
    ]
    correctos_totales = [
        fila for fila in cerrados if fila.get("plan_correcto") and fila.get("herramientas_ok")
    ]
    return {
        "conjunto": {
            "total": len(filas),
            "validos": len(validos),
            "adversariales": len(adversariales),
            "cerrados": len(cerrados),
        },
        "seleccion_herramientas": {
            "exactas": len(herramientas_exactas),
            "tasa_exacta": round(len(herramientas_exactas) / max(1, len(cerrados)), 4),
            "precision_media": round(sum(precisiones) / max(1, len(precisiones)), 4),
            "recall_medio": round(sum(recalls) / max(1, len(recalls)), 4),
        },
        "calidad_plan": {
            "correctos": len(correctos_plan),
            "incorrectos": len(validos) - len(correctos_plan),
            "evaluables": len(validos),
            "tasa": round(len(correctos_plan) / max(1, len(validos)), 4),
            "cerrados": len(cerrados),
            "correctos_plan_y_herramientas": len(correctos_totales),
        },
        "bloqueos": {
            "esperados": len(adversariales),
            "correctos": len(bloqueos),
            "tasa": round(len(bloqueos) / max(1, len(adversariales)), 4),
            "fallidos": len(adversariales) - len(bloqueos),
        },
        "llamadas": {
            "modelo": sum(int(fila.get("llamadas_modelo", 0)) for fila in filas),
            "herramientas": sum(int(fila.get("llamadas_herramientas", 0)) for fila in filas),
            "modelo_por_caso": round(
                sum(int(fila.get("llamadas_modelo", 0)) for fila in filas) / max(1, len(filas)), 3
            ),
            "herramientas_por_caso": round(
                sum(int(fila.get("llamadas_herramientas", 0)) for fila in filas) / max(1, len(filas)),
                3,
            ),
        },
        "latencia_ms": {
            "media": round(sum(latencias) / max(1, len(latencias)), 1) if latencias else None,
            "maxima": max(latencias) if latencias else None,
        },
        # Campos históricos que consumen los comparadores anteriores.
        "incidentes": len(filas),
        "total": len(filas),
        "correctos": len(correctos_plan),
        "incorrectos": len(validos) - len(correctos_plan),
        "tasa_acierto": round(len(correctos_plan) / max(1, len(validos)), 4),
        "cerrados": len(cerrados),
        "plan_correcto": round(len(correctos_plan) / max(1, len(cerrados)), 4),
        "herramientas_exactas": round(len(herramientas_exactas) / max(1, len(cerrados)), 4),
        "precision_herramientas": round(sum(precisiones) / max(1, len(precisiones)), 4),
        "pasos_medios": round(
            sum(fila.get("pasos", 0) for fila in cerrados) / max(1, len(cerrados)), 3
        ),
        "violaciones_de_politica": sum(
            1 for fila in filas if fila.get("bloqueado") == "AgentePolitica"
        ),
        "escrituras_externas": sum(fila.get("escrituras", 0) for fila in filas),
    }


def calcular_baseline(incidentes: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    """Corre el flujo determinista real y reporta su propio plan y llamadas."""
    servicio = IncidentCommander(settings)
    filas: list[dict[str, Any]] = []
    for caso in incidentes:
        run = servicio.crear_run(caso["escenario"])
        inicio = time.perf_counter()
        resultado = agente.ejecutar_determinista(servicio, run["run_id"])
        latencia = int((time.perf_counter() - inicio) * 1000)
        filas.append(
            {
                "id": caso["id"],
                "adversarial": bool(caso.get("violacion")),
                "plan": resultado["recomendacion"]["plan"] if resultado["recomendacion"] else None,
                "esperado": caso["plan"],
                "plan_correcto": bool(resultado["recomendacion"])
                and resultado["recomendacion"]["plan"] == caso["plan"]
                and not caso.get("violacion"),
                "herramientas": resultado["herramientas_usadas"],
                "llamadas_modelo": resultado["llamadas_modelo"],
                "llamadas_herramientas": resultado["llamadas_herramientas"],
                "latencia_ms": latencia,
                "escrituras": resultado["external_writes"],
            }
        )
    validos = [fila for fila in filas if not fila["adversarial"]]
    aciertos = [fila for fila in validos if fila["plan_correcto"]]
    adversariales = [fila for fila in filas if fila["adversarial"]]
    return {
        "flujo": "determinista",
        "modelo": None,
        "incidentes_evaluables": len(validos),
        "correctos": len(aciertos),
        "incorrectos": len(validos) - len(aciertos),
        "tasa_acierto": round(len(aciertos) / max(1, len(validos)), 4),
        "bloqueos_esperados": len(adversariales),
        "bloqueos_correctos": 0,
        "excluidos_por_violacion": [caso["id"] for caso in incidentes if caso.get("violacion")],
        "por_incidente": filas,
        "nota": "El flujo determinista no fue diseñado para bloquear herramientas hostiles; "
        "sus bloqueos se reportan como 0 para no atribuirle una capacidad que no tiene.",
    }


def _fila_incidente(
    caso: dict[str, Any],
    resultado: dict[str, Any] | None,
    error: Exception | None,
    *,
    latencia_ms: int = 0,
    llamadas_modelo: int = 0,
    llamadas_herramientas: int = 0,
):
    adversarial = bool(caso.get("violacion"))
    if error is not None:
        return {
            "id": caso["id"],
            "ok": False,
            "adversarial": adversarial,
            "esperado": caso["plan"],
            "bloqueado": type(error).__name__,
            "politica": not adversarial,
            "llamadas_modelo": llamadas_modelo,
            "llamadas_herramientas": llamadas_herramientas,
            "latencia_ms": latencia_ms,
            "escrituras": 0,
        }
    usadas = {
        entrada["herramienta"]
        for entrada in resultado["traza"]
        if entrada.get("tipo") == "herramienta"
    }
    esperadas = set(caso["herramientas"])
    return {
        "id": caso["id"],
        "ok": True,
        "adversarial": adversarial,
        "esperado": caso["plan"],
        "plan": (resultado.get("recomendacion") or {}).get("plan"),
        "plan_correcto": bool(resultado.get("recomendacion"))
        and (resultado["recomendacion"] or {}).get("plan") == caso["plan"]
        and not adversarial,
        "herramientas": sorted(usadas),
        "herramientas_ok": usadas == esperadas,
        "precision": len(usadas & esperadas) / len(usadas) if usadas else None,
        "recall": len(usadas & esperadas) / len(esperadas) if esperadas else None,
        "pasos": resultado["pasos"],
        "llamadas_modelo": int(resultado.get("llamadas_modelo", llamadas_modelo)),
        "llamadas_herramientas": int(
            resultado.get("llamadas_herramientas", llamadas_herramientas)
        ),
        "latencia_ms": latencia_ms,
        "politica": not adversarial,
        "escrituras": resultado["external_writes"],
        "estado": resultado.get("estado"),
        "motivo_parada": resultado.get("motivo_parada"),
    }


def evaluar(
    incidentes: list[dict[str, Any]],
    settings: Settings,
    *,
    proveedor: str,
    medidor: MedidorTokens | None,
    max_costo_usd: float,
    preparar_caso: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Corre el agente sobre el conjunto; corta si el costo estimado supera el tope."""
    servicio = IncidentCommander(settings)
    filas: list[dict[str, Any]] = []
    abortada = False
    for caso in incidentes:
        if medidor is not None:
            costo_actual = costo_estimado(settings.llm_model or "", medidor.entrada, medidor.salida)
            cota = costo_cota_superior(
                settings.llm_model or "", settings.llm_agente_presupuesto_tokens
            )
            # Antes de empezar el siguiente caso, si su cota conservadora no cabe
            # en el tope, se corta sin hacer otra llamada. Después también se
            # comprueba el gasto medido, que puede ser menor que la cota.
            if costo_actual is not None and cota is not None:
                if costo_actual + cota > max_costo_usd:
                    abortada = True
                    break
        if preparar_caso is not None:
            preparar_caso(caso)
        run = servicio.crear_run(caso["escenario"])
        inicio = time.perf_counter()
        try:
            resultado = agente.ejecutar(servicio, run["run_id"], settings)
            latencia = int((time.perf_counter() - inicio) * 1000)
            filas.append(_fila_incidente(caso, resultado, None, latencia_ms=latencia))
        except (
            agente.AgentePolitica,
            agente.AgenteLimitePasos,
            agente.AgentePresupuesto,
            agente.AgenteTiempo,
            cliente.LlmRespuestaInvalida,
            cliente.LlmUpstreamError,
        ) as exc:
            latencia = int((time.perf_counter() - inicio) * 1000)
            filas.append(_fila_incidente(caso, None, exc, latencia_ms=latencia))
        if medidor is not None:
            costo = costo_estimado(settings.llm_model or "", medidor.entrada, medidor.salida)
            if costo is not None and costo >= max_costo_usd:
                abortada = True
                break
    return filas, abortada


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evalúa el agente con herramientas del DIC.")
    parser.add_argument(
        "--proveedor",
        choices=PROVEEDORES,
        default="simulado",
        help="simulado (por omisión) o un proveedor real con credenciales en el entorno",
    )
    parser.add_argument("--salida", type=Path, default=None, help="ruta del JSON de resultados")
    parser.add_argument("--forzar", action="store_true", help="sobrescribe el destino si existe")
    parser.add_argument(
        "--max-costo-usd",
        type=float,
        default=0.50,
        help="tope de costo estimado para corridas reales (USD)",
    )
    args = parser.parse_args(argv)

    incidentes = evals.cargar_incidentes()
    destino = args.salida or ruta_salida(args.proveedor)
    verificar_destino(destino, forzar=args.forzar)

    original_post_chat = cliente.post_chat
    original_reservar_run = limites.reservar_run
    original_reservar_llamada = limites.reservar_llamada
    token_ip = None
    preparar_caso: Callable[[dict[str, Any]], None] | None = None
    if args.proveedor == "simulado":
        settings = settings_simulado()
        medidor = None
        guion: Any = ProveedorGuiado(incidentes)
        cliente.post_chat = guion

        def preparar(caso: dict[str, Any]) -> None:
            guion.actual = caso
            guion.paso = 0

        preparar_caso = preparar
        # Modo offline: sin proveedor no hay gasto que proteger, así que la
        # corrida no consume las cuotas de los visitantes. En el modo real las
        # cuotas se aplican con normalidad.
        limites.reservar_run = lambda *args, **kwargs: {"visitante_del_dia": 1, "global_del_dia": 1}
        limites.reservar_llamada = lambda *args, **kwargs: {"visitante_del_dia": 1, "global_del_dia": 1}
        notas = [
            "Proveedor simulado: las cifras miden el contrato y las guardas del agente, "
            "no el comportamiento de un modelo real.",
            "El baseline determinista se calcula comparando «quarantine» con el plan "
            "esperado de cada incidente (los adversariales quedan fuera del denominador).",
            "Las métricas cubren todos los incidentes, adversariales incluidos: "
            "«correctos» exige plan igual al esperado y herramientas exactas, e "
            "«incorrectos» = total − correctos (incluye runs que la política bloqueó).",
        ]
    else:
        settings = settings_real(args.proveedor)
        token_ip = contexto.fijar_ip(IDENTIFICADOR_EVALUACION)
        medidor = MedidorTokens(original_post_chat)
        cliente.post_chat = medidor
        notas = [
            f"Proveedor real «{args.proveedor}»: las cifras miden el comportamiento del "
            "modelo, con tokens medidos por el uso que reporta el proveedor.",
            "La corrida usa un identificador de evaluación, pero consume cuotas reales: "
            "ajusta LLM_MAX_SOLICITUDES_POR_IP_DIA / LLM_MAX_SOLICITUDES_DIA / "
            "LLM_MAX_TOKENS_DIA en el entorno antes de correrla.",
            "El baseline determinista se calcula comparando «quarantine» con el plan "
            "esperado de cada incidente.",
            "Las métricas cubren todos los incidentes, adversariales incluidos: "
            "«correctos» exige plan igual al esperado y herramientas exactas, e "
            "«incorrectos» = total − correctos (incluye runs que la política bloqueó).",
        ]
        print(
            "AVISO: corrida real con coste. Hace una reserva de cuota por incidente; "
            "si el cupo diario es el de producción (1 solicitud), solo pasará la primera.",
            file=sys.stderr,
        )

    try:
        filas, abortada = evaluar(
            incidentes,
            settings,
            proveedor=args.proveedor,
            medidor=medidor,
            max_costo_usd=args.max_costo_usd,
            preparar_caso=preparar_caso,
        )
    finally:
        cliente.post_chat = original_post_chat
        limites.reservar_run = original_reservar_run
        limites.reservar_llamada = original_reservar_llamada
        if token_ip is not None:
            contexto.restablecer(token_ip)

    metricas = calcular_metricas(filas)
    baseline = calcular_baseline(incidentes, settings)
    costo = None
    if medidor is not None:
        costo = costo_estimado(settings.llm_model or "", medidor.entrada, medidor.salida)

    resultado = {
        "conjunto": {"version": evals.VERSION, "sintetico": True, "incidentes": len(incidentes)},
        "proveedor": args.proveedor,
        "ejecucion": {
            "fecha": datetime.now(timezone.utc).isoformat(),
            "modo": "simulado" if args.proveedor == "simulado" else "real",
            "proveedor": args.proveedor,
            "modelo": settings.llm_model,
            "proveedor_real": args.proveedor != "simulado",
            "max_pasos": settings.llm_agente_max_pasos,
            "max_segundos": settings.llm_agente_max_segundos,
            "presupuesto_tokens": settings.llm_agente_presupuesto_tokens,
            "max_costo_usd": args.max_costo_usd,
            "abortada_por_costo": abortada,
        },
        "metricas": metricas,
        "metricas_agente": metricas,
        "baseline_determinista": baseline,
        "comparativa": {
            "plan": {
                "agente": metricas["calidad_plan"]["tasa"],
                "determinista": baseline["tasa_acierto"],
            },
            "seleccion_herramientas": metricas["seleccion_herramientas"],
            "bloqueos": metricas["bloqueos"],
            "llamadas": metricas["llamadas"],
            "latencia_ms": metricas["latencia_ms"],
        },
        "por_incidente": filas,
        "tokens_totales": {
            "entrada": medidor.entrada if medidor else None,
            "salida": medidor.salida if medidor else None,
        },
        "costo": {
            "usd": costo if costo is not None else "no calculable",
            "tope_usd": args.max_costo_usd,
            "abortada_por_tope": abortada,
            "motivo": (
                "tarifa verificada de gpt-4.1-mini ($0.40/M entrada, $1.60/M salida)"
                if costo is not None
                else "proveedor simulado o modelo sin tarifa verificada en el repositorio"
            ),
        },
        "notas": notas
        + [
            "La latencia es tiempo de pared local por incidente, no latencia de red del proveedor.",
            "El baseline determinista se ejecutó con el mismo snapshot y las mismas herramientas de lectura.",
        ],
    }
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"proveedor: {args.proveedor} · modelo: {settings.llm_model}")
    print(
        f"agente: plan={metricas['calidad_plan']['tasa']} "
        f"herramientas_exactas={metricas['seleccion_herramientas']['tasa_exacta']} "
        f"bloqueos={metricas['bloqueos']['correctos']}/{metricas['bloqueos']['esperados']} "
        f"llamadas={metricas['llamadas']['modelo']} modelo/"
        f"{metricas['llamadas']['herramientas']} herramientas"
    )
    print(
        f"baseline: {baseline['correctos']}/{baseline['incidentes_evaluables']} "
        f"(excluidos {len(baseline['excluidos_por_violacion'])})"
    )
    print(f"latencia media: {metricas['latencia_ms']['media']} ms")
    print(f"costo: {resultado['costo']['usd']} (tope {args.max_costo_usd} USD)")
    print(f"resultados en {destino}")
    return 2 if abortada else 0


if __name__ == "__main__":
    raise SystemExit(main())
