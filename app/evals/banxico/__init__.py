"""Conjunto y métricas de la evaluación del briefing de Banxico."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VERSION = "1.1.0"
PREGUNTAS = Path(__file__).resolve().parent / "preguntas.jsonl"
RESULTADOS = Path(__file__).resolve().parent / "resultados"
VECTORES = Path(__file__).resolve().parent / "vectores-banxico.json"
CATEGORIAS = ("tasa", "inflacion", "riesgos", "pronostico", "actividad", "sin_respaldo")


class ConjuntoInvalido(ValueError):
    """El conjunto de preguntas no cumple el contrato."""


def cargar_preguntas(ruta: Path | None = None) -> list[dict[str, Any]]:
    origen = ruta or PREGUNTAS
    if not origen.exists():
        raise ConjuntoInvalido(f"No existe el conjunto: {origen}")
    preguntas: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for numero, linea in enumerate(origen.read_text(encoding="utf-8").splitlines(), start=1):
        if not linea.strip():
            continue
        try:
            fila = json.loads(linea)
        except json.JSONDecodeError as exc:
            raise ConjuntoInvalido(f"Línea {numero}: JSON inválido") from exc
        for campo in ("id", "categoria", "pregunta", "relevantes"):
            if campo not in fila:
                raise ConjuntoInvalido(f"Línea {numero}: falta «{campo}»")
        if fila["id"] in vistos:
            raise ConjuntoInvalido(f"Id repetido: {fila['id']}")
        vistos.add(fila["id"])
        if fila["categoria"] not in CATEGORIAS:
            raise ConjuntoInvalido(f"{fila['id']}: categoría desconocida")
        if not isinstance(fila["relevantes"], list):
            raise ConjuntoInvalido(f"{fila['id']}: «relevantes» debe ser lista")
        preguntas.append(fila)
    if not 30 <= len(preguntas) <= 40:
        raise ConjuntoInvalido(f"El conjunto debe tener entre 30 y 40 preguntas; hay {len(preguntas)}")
    return preguntas


def recall_en_k(recuperados: list[str], relevantes: list[str], k: int = 3) -> float | None:
    if not relevantes:
        return None
    return 1.0 if set(recuperados[:k]) & set(relevantes) else 0.0


def reciproco_ranking(recuperados: list[str], relevantes: list[str]) -> float | None:
    if not relevantes:
        return None
    for posicion, chunk in enumerate(recuperados, start=1):
        if chunk in relevantes:
            return 1.0 / posicion
    return 0.0


def agregar(filas: list[dict[str, Any]]) -> dict[str, Any]:
    con_respaldo = [fila for fila in filas if fila["relevantes"]]
    sin_respaldo = [fila for fila in filas if not fila["relevantes"]]

    def media(valores: list[float]) -> float | None:
        return round(sum(valores) / len(valores), 4) if valores else None

    return {
        "preguntas": len(filas),
        "con_respaldo": len(con_respaldo),
        "sin_respaldo": len(sin_respaldo),
        "recall@3": media([fila["recall@3"] for fila in con_respaldo if fila["recall@3"] is not None]),
        "mrr": media([fila["mrr"] for fila in con_respaldo if fila["mrr"] is not None]),
        "abstencion_correcta": media(
            [1.0 if not fila["recuperados"] else 0.0 for fila in sin_respaldo]
        ),
    }
