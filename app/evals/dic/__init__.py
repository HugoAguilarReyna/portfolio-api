"""Conjunto y métricas de la evaluación del agente de Incident Commander."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VERSION = "1.0.0"
INCIDENTES = Path(__file__).resolve().parent / "incidentes.jsonl"
RESULTADOS = Path(__file__).resolve().parent / "resultados"


class ConjuntoInvalido(ValueError):
    """El conjunto de incidentes no cumple el contrato."""


def cargar_incidentes(ruta: Path | None = None) -> list[dict[str, Any]]:
    origen = ruta or INCIDENTES
    if not origen.exists():
        raise ConjuntoInvalido(f"No existe el conjunto: {origen}")
    filas: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for numero, linea in enumerate(origen.read_text(encoding="utf-8").splitlines(), start=1):
        if not linea.strip():
            continue
        try:
            fila = json.loads(linea)
        except json.JSONDecodeError as exc:
            raise ConjuntoInvalido(f"Línea {numero}: JSON inválido") from exc
        for campo in ("id", "escenario", "herramientas", "plan", "justificacion", "evidencia", "riesgos"):
            if campo not in fila:
                raise ConjuntoInvalido(f"Línea {numero}: falta «{campo}»")
        if fila["id"] in vistos:
            raise ConjuntoInvalido(f"Id repetido: {fila['id']}")
        vistos.add(fila["id"])
        if fila["escenario"] not in ("missing", "duplicate", "schema"):
            raise ConjuntoInvalido(f"{fila['id']}: escenario desconocido")
        if fila["plan"] not in ("quarantine", "backfill", "fullreload"):
            raise ConjuntoInvalido(f"{fila['id']}: plan fuera de política")
        filas.append(fila)
    if not 10 <= len(filas) <= 15:
        raise ConjuntoInvalido(f"El conjunto debe tener entre 10 y 15 incidentes; hay {len(filas)}")
    return filas
