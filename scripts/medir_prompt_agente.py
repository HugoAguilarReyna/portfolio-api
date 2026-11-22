"""Mide caracteres y tokens estimados del prompt del agente, SIN llamar a OpenAI.

Construye los mensajes reales del bucle (SISTEMA + esquemas de herramientas +
contexto del run + resultados de herramientas) y calcula el peor caso de 6 pasos.
Si `tiktoken` está disponible usa su codificación; si no, estima con
caracteres/3.6 y lo marca como estimación (no es una medición).

    python scripts/medir_prompt_agente.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app.config import Settings  # noqa: E402
from app.incident_commander import agente, herramientas  # noqa: E402
from app.incident_commander.service import IncidentCommander  # noqa: E402
from app.llm import cliente  # noqa: E402

DESTINO = RAIZ / "app" / "evals" / "dic" / "mediciones"
#: Tope del resultado de herramienta dentro del historial (agente.py lo corta a 2000).
TOPE_RESULTADO = 2000
#: Tope del contexto serializado (cliente.recortar usa 6000 por omisión).
TOPE_CONTEXTO = cliente.MAXIMO_CONTEXTO


def codificador():
    """Devuelve (función de conteo, método). tiktoken si está; si no, chars/3.6."""
    try:
        import tiktoken  # type: ignore

        codificacion = tiktoken.get_encoding("cl100k_base")
        return (lambda texto: len(codificacion.encode(texto))), "tiktoken-cl100k_base"
    except Exception:  # noqa: BLE001 - sin red o sin paquete
        return (lambda texto: max(1, round(len(texto) / 3.6))), "estimacion-chars/3.6"


def contexto_real(servicio: IncidentCommander, run_id: str) -> str:
    run = servicio._run(run_id)
    return cliente.recortar(servicio._contexto_consulta(run))


def contexto_peor_caso(base: str) -> str:
    """Lleva el contexto al tope conservando la estructura (cota superior)."""
    if len(base) >= TOPE_CONTEXTO:
        return base
    relleno = "x" * (TOPE_CONTEXTO - len(base) - 2)
    return '{"relleno":"' + relleno + '"}'


def resultado_herramienta(servicio: IncidentCommander, run_id: str) -> str:
    """Serialización real de una herramienta, truncada al tope del historial."""
    datos = herramientas.ejecutar(servicio, run_id, "validaciones", {})
    return json.dumps(datos, ensure_ascii=False)[:TOPE_RESULTADO]


def medir(etiqueta: str, contexto: str, resultados: str, contar) -> dict[str, Any]:
    sistema = agente.SISTEMA
    esquemas = json.dumps(herramientas.esquemas(), ensure_ascii=False)
    fijo = sistema + esquemas
    pasos: list[dict[str, Any]] = []
    acumulado = 0
    for paso in range(1, 7):
        historial = ""
        for previo in range(1, paso):
            historial += json.dumps(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call-{previo}",
                            "type": "function",
                            "function": {"name": "validaciones", "arguments": "{}"},
                        }
                    ],
                },
                ensure_ascii=False,
            )
            historial += json.dumps(
                {"role": "tool", "tool_call_id": f"call-{previo}", "content": resultados},
                ensure_ascii=False,
            )
        prompt_chars = len(fijo) + len(contexto) + len(historial)
        prompt_tokens = contar(fijo) + contar(contexto) + contar(historial)
        acumulado += prompt_tokens
        salida = min(int(Settings(_env_file=None).llm_max_tokens), 300)
        pasos.append(
            {
                "paso": paso,
                "chars_prompt": prompt_chars,
                "tokens_prompt": prompt_tokens,
                "tokens_salida_tope": salida,
                "acumulado_prompt": acumulado,
            }
        )
    total = acumulado + sum(paso["tokens_salida_tope"] for paso in pasos)
    return {
        "caso": etiqueta,
        "fijo_sistema_y_esquemas": {"chars": len(fijo), "tokens": contar(fijo)},
        "contexto": {"chars": len(contexto), "tokens": contar(contexto)},
        "resultado_herramienta": {"chars": len(resultados), "tokens": contar(resultados)},
        "pasos": pasos,
        "tokens_prompt_6_pasos": acumulado,
        "tokens_totales_peor_caso": total,
    }


def main() -> int:
    servicio = IncidentCommander(Settings(_env_file=None))
    run = servicio.crear_run("duplicate")
    contar, metodo = codificador()

    real = medir("contexto-real", contexto_real(servicio, run["run_id"]), resultado_herramienta(servicio, run["run_id"]), contar)
    tope = medir("contexto-al-tope", contexto_peor_caso(contexto_real(servicio, run["run_id"])), "x" * TOPE_RESULTADO, contar)
    provisional = tope["tokens_totales_peor_caso"]
    sugerido = ((provisional + 499) // 500) * 500

    resultado = {
        "fecha": datetime.now(timezone.utc).isoformat(),
        "metodo_conteo": metodo,
        "es_medicion_real": metodo.startswith("tiktoken"),
        "nota": (
            "Sin llamada a OpenAI. Si el método es chars/3.6 es una ESTIMACIÓN, "
            "no un conteo del tokenizador del modelo."
        ),
        "topes": {"contexto_chars": TOPE_CONTEXTO, "resultado_herramienta_chars": TOPE_RESULTADO, "pasos": 6},
        "caso_real": real,
        "caso_peor": tope,
        "presupuesto_provisional_sugerido": sugerido,
        "presupuesto_actual": int(Settings(_env_file=None).llm_agente_presupuesto_tokens),
        "max_tokens_dia_sugerido": sugerido * 10,
        "coherencia": tope["tokens_totales_peor_caso"] <= sugerido,
    }
    DESTINO.mkdir(parents=True, exist_ok=True)
    archivo = DESTINO / f"agente-prompt-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json"
    archivo.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"método: {metodo}")
    print(f"caso real : prompt 6 pasos = {real['tokens_prompt_6_pasos']} tokens, total con salida = {real['tokens_totales_peor_caso']}")
    print(f"caso tope : prompt 6 pasos = {tope['tokens_prompt_6_pasos']} tokens, total con salida = {tope['tokens_totales_peor_caso']}")
    for paso in tope["pasos"]:
        print(f"  paso {paso['paso']}: prompt {paso['tokens_prompt']} tokens (acumulado {paso['acumulado_prompt']})")
    print(f"presupuesto actual: {resultado['presupuesto_actual']} · sugerido provisional: {sugerido}")
    print(f"escrito en {archivo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
