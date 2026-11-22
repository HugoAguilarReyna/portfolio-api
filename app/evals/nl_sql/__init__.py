"""Conjunto y métricas de la evaluación de NL-to-SQL.

El contrato de referencia es el del sistema, no el del modelo:

* indicadores del catálogo cerrado (`METRICS`);
* años dentro de 2019–2025;
* operaciones `trend`, `comparison`, `max` y `min`;
* cualquier país distinto de México queda fuera del alcance.

El conjunto es sintético (preguntas escritas a mano) y no contiene datos
privados. Los resultados se guardan junto al conjunto, versionados.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VERSION = "1.0.0"
CASOS = Path(__file__).resolve().parent / "casos.jsonl"
RESULTADOS = Path(__file__).resolve().parent / "resultados"
CATEGORIAS = ("directa", "parafraseada", "no_soportada", "adversarial")
PREGUNTA_MIN = 5
PREGUNTA_MAX = 500
OPERACIONES = ("trend", "comparison", "max", "min")


class ConjuntoInvalido(ValueError):
    """El fichero de casos no cumple el contrato del conjunto."""


def cargar_casos(ruta: Path | None = None) -> list[dict[str, Any]]:
    origen = ruta or CASOS
    if not origen.exists():
        raise ConjuntoInvalido(f"No existe el conjunto: {origen}")
    casos: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for numero, linea in enumerate(origen.read_text(encoding="utf-8").splitlines(), start=1):
        if not linea.strip():
            continue
        try:
            fila = json.loads(linea)
        except json.JSONDecodeError as exc:
            raise ConjuntoInvalido(f"Línea {numero}: JSON inválido") from exc
        for campo in ("id", "categoria", "pregunta", "esperado"):
            if campo not in fila:
                raise ConjuntoInvalido(f"Línea {numero}: falta «{campo}»")
        if fila["id"] in vistos:
            raise ConjuntoInvalido(f"Id repetido: {fila['id']}")
        vistos.add(fila["id"])
        if fila["categoria"] not in CATEGORIAS:
            raise ConjuntoInvalido(f"{fila['id']}: categoría desconocida")
        if not PREGUNTA_MIN <= len(fila["pregunta"]) <= PREGUNTA_MAX:
            raise ConjuntoInvalido(
                f"{fila['id']}: la pregunta debe medir entre {PREGUNTA_MIN} y {PREGUNTA_MAX} caracteres"
            )
        esperado = fila["esperado"]
        if esperado.get("soporte") == "soportado":
            if not esperado.get("indicadores"):
                raise ConjuntoInvalido(f"{fila['id']}: sin indicadores esperados")
            if esperado.get("operacion") not in OPERACIONES:
                raise ConjuntoInvalido(f"{fila['id']}: operación desconocida")
        elif esperado.get("soporte") != "no_soportado":
            raise ConjuntoInvalido(f"{fila['id']}: soporte inválido")
        casos.append(fila)
    if not 40 <= len(casos) <= 60:
        raise ConjuntoInvalido(f"El conjunto debe tener entre 40 y 60 casos; hay {len(casos)}")
    return casos


def operacion_de(plan: dict[str, Any] | None) -> str | None:
    """Traduce el plan interno del servicio a la operación del contrato."""
    if not isinstance(plan, dict):
        return None
    tipo = plan.get("kind")
    if tipo == "ranking":
        return "max" if plan.get("rank_order") == "DESC" else "min"
    if tipo in {"trend", "comparison"}:
        return tipo
    return None


def plan_de(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        return None
    operacion = operacion_de(plan)
    codigos = plan.get("indicator_codes") or []
    inicio = plan.get("start_year")
    fin = plan.get("end_year")
    if operacion is None or not codigos or not isinstance(inicio, int) or not isinstance(fin, int):
        return None
    return {
        "indicadores": sorted(str(codigo) for codigo in codigos),
        "anio_inicio": inicio,
        "anio_fin": fin,
        "operacion": operacion,
    }


def comparar(plan: dict[str, Any] | None, esperado: dict[str, Any]) -> dict[str, Any]:
    """Compara un plan obtenido con el resultado esperado del contrato."""
    if esperado.get("soporte") == "no_soportado":
        return {
            "soporte_ok": plan is None,
            "acierto_exacto": plan is None,
            "indicadores_ok": None,
            "anios_ok": None,
            "operacion_ok": None,
        }
    obtenido = plan_de(plan)
    if obtenido is None:
        return {
            "soporte_ok": False,
            "acierto_exacto": False,
            "indicadores_ok": False,
            "anios_ok": False,
            "operacion_ok": False,
        }
    indicadores = obtenido["indicadores"] == sorted(esperado["indicadores"])
    anios = (obtenido["anio_inicio"], obtenido["anio_fin"]) == (
        esperado["anio_inicio"],
        esperado["anio_fin"],
    )
    operacion = obtenido["operacion"] == esperado["operacion"]
    return {
        "soporte_ok": True,
        "acierto_exacto": indicadores and anios and operacion,
        "indicadores_ok": indicadores,
        "anios_ok": anios,
        "operacion_ok": operacion,
    }


def agregar(resultados: list[dict[str, Any]]) -> dict[str, Any]:
    """Métricas agregadas: acierto, precisión por campo, abstenciones y latencia."""
    soportados = [fila for fila in resultados if fila["esperado_soporte"] == "soportado"]
    no_soportados = [fila for fila in resultados if fila["esperado_soporte"] == "no_soportado"]

    def media(valores: list[float]) -> float | None:
        return round(sum(valores) / len(valores), 4) if valores else None

    latencias = sorted(fila["latencia_ms"] for fila in resultados)
    mediana = latencias[len(latencias) // 2] if latencias else None
    return {
        "casos": len(resultados),
        "casos_soportados": len(soportados),
        "casos_no_soportados": len(no_soportados),
        "acierto_exacto": media([1.0 if fila["acierto_exacto"] else 0.0 for fila in soportados]),
        "precision_indicadores": media(
            [1.0 if fila["indicadores_ok"] else 0.0 for fila in soportados]
        ),
        "precision_anios": media([1.0 if fila["anios_ok"] else 0.0 for fila in soportados]),
        "precision_operacion": media(
            [1.0 if fila["operacion_ok"] else 0.0 for fila in soportados]
        ),
        "deteccion_no_soportado": media(
            [1.0 if fila["soporte_ok"] else 0.0 for fila in no_soportados]
        ),
        "falsos_soportados": sum(
            1 for fila in no_soportados if fila["esperado_soporte"] == "no_soportado" and not fila["soporte_ok"]
        ),
        "latencia_media_ms": media([fila["latencia_ms"] for fila in resultados]),
        "latencia_mediana_ms": mediana,
        "tokens_totales": sum(fila.get("tokens") or 0 for fila in resultados),
    }


def por_categoria(resultados: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    salida: dict[str, dict[str, Any]] = {}
    for categoria in CATEGORIAS:
        grupo = [fila for fila in resultados if fila["categoria"] == categoria]
        if not grupo:
            continue
        salida[categoria] = {
            "casos": len(grupo),
            "acierto_exacto": round(sum(1 for fila in grupo if fila["acierto_exacto"]) / len(grupo), 4),
            "no_soportado_ok": round(sum(1 for fila in grupo if fila["soporte_ok"]) / len(grupo), 4),
        }
    return salida
