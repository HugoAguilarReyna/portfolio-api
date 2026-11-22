"""Recuperación por embeddings e híbrida para el briefing de Banxico.

El corpus es pequeño (7 fragmentos, dos comunicados oficiales), así que basta
NumPy y similitud coseno: no se añade infraestructura vectorial.

Hay dos fuentes de vectores, explícitas y separadas:

* `embeddings-locales`: bolsa de términos ponderada (TF, L2), calculada aquí y
  reproducible sin red. Sirve para *comparar métodos* en la evaluación; **no es
  un modelo de embeddings** y no debe presentarse como tal.
* `embeddings-openai`: vectores versionados en un JSON que genera
  `scripts/generar_embeddings_banxico.py` con una llamada real (coste). No se
  versionan sin revisar formato, tamaño y conveniencia.

La recuperación léxica (`puntuar`) se conserva como baseline y fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from app.rag_banxico.service import terminos

MODOS = ("lexica", "embeddings-locales", "embeddings-openai", "hibrida")
UMBRAL_COSENO = 0.20
UMBRAL_HIBRIDO = 0.25
ALFA_HIBRIDA = 0.5


def _bolsa() -> tuple[dict[str, int], np.ndarray]:
    return {}, np.zeros(0)


def vocabulario(fragmentos: list[dict[str, Any]]) -> list[str]:
    """Vocabulario ordenado del corpus (reproducible)."""
    vistos: set[str] = set()
    for fila in fragmentos:
        vistos.update(terminos(str(fila.get("heading", ""))))
        vistos.update(terminos(str(fila.get("content", ""))))
    return sorted(vistos)


def vectorizar(texto: str, vocabulario_: list[str]) -> np.ndarray:
    """TF con normalización L2 sobre el vocabulario del corpus."""
    indice = {termino: posicion for posicion, termino in enumerate(vocabulario_)}
    vector = np.zeros(len(vocabulario_), dtype=np.float64)
    for termino in terminos(texto):
        posicion = indice.get(termino)
        if posicion is not None:
            vector[posicion] += 1.0
    norma = float(np.linalg.norm(vector))
    return vector / norma if norma else vector


def coseno(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


def cargar_vectores(ruta: Path) -> dict[str, list[float]] | None:
    """Vectores reales versionados (`embeddings-openai`), si existen."""
    if not ruta.exists():
        return None
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    if not isinstance(datos, dict) or "vectores" not in datos:
        return None
    return {str(clave): list(valor) for clave, valor in datos["vectores"].items()}


def _lexico(pregunta: str, fragmentos: list[dict[str, Any]]) -> list[tuple[dict[str, Any], float]]:
    """Puntuación léxica simple de todos los fragmentos (para el híbrido)."""
    consulta = set(terminos(pregunta))
    filas: list[tuple[dict[str, Any], float]] = []
    for fila in fragmentos:
        contenido = set(terminos(str(fila.get("heading", "")) + " " + str(fila.get("content", ""))))
        if not consulta or not contenido:
            filas.append((fila, 0.0))
            continue
        filas.append((fila, len(consulta & contenido) / len(consulta)))
    return filas


def _top(filas: list[tuple[dict[str, Any], float]], limite: int = 3) -> list[dict[str, Any]]:
    ordenadas = sorted(filas, key=lambda par: (-par[1], str(par[0].get("chunk_id"))))
    return [
        {**fila, "score": round(float(puntuacion), 6)}
        for fila, puntuacion in ordenadas[:limite]
    ]


def recuperar_lexica(pregunta: str, fragmentos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Baseline léxico: solo devuelve fragmentos con coincidencia real."""
    return _top([(fila, score) for fila, score in _lexico(pregunta, fragmentos) if score > 0])


def recuperar_embeddings(
    pregunta: str,
    fragmentos: list[dict[str, Any]],
    *,
    umbral: float = UMBRAL_COSENO,
) -> list[dict[str, Any]]:
    """Top-3 por coseno con vectores locales; descarta lo que no supera el umbral."""
    vocabulario_ = vocabulario(fragmentos)
    consulta = vectorizar(pregunta, vocabulario_)
    filas = [
        (fila, coseno(consulta, vectorizar(f"{fila.get('heading', '')} {fila.get('content', '')}", vocabulario_)))
        for fila in fragmentos
    ]
    return _top([(fila, puntuacion) for fila, puntuacion in filas if puntuacion >= umbral])


def recuperar_embeddings_reales(
    pregunta: str,
    fragmentos: list[dict[str, Any]],
    vectores: dict[str, list[float]],
) -> list[dict[str, Any]]:
    """Top-3 con vectores reales (misma dimensionalidad que el vector de consulta)."""
    referencia = next(iter(vectores.values()), None)
    if not referencia:
        return []
    # Sin la consulta vectorizada por el mismo modelo no se puede puntuar: el
    # comando de generación guarda también los vectores de evaluación.
    consulta = vectores.get("__consulta__:" + pregunta)
    if consulta is None:
        return []
    consulta_v = np.array(consulta, dtype=np.float64)
    filas = []
    for fila in fragmentos:
        vector = vectores.get(str(fila.get("chunk_id")))
        if isinstance(vector, list) and len(vector) == len(consulta_v):
            filas.append((fila, coseno(consulta_v, np.array(vector, dtype=np.float64))))
    return _top(filas)


def recuperar_hibrida(
    pregunta: str,
    fragmentos: list[dict[str, Any]],
    *,
    alfa: float = ALFA_HIBRIDA,
    umbral: float = UMBRAL_HIBRIDO,
) -> list[dict[str, Any]]:
    """Mezcla lineal de la señal léxica y el coseno, ambas normalizadas."""
    lexical = _lexico(pregunta, fragmentos)
    maximo = max((p for _, p in lexical), default=0.0) or 1.0
    vocabulario_ = vocabulario(fragmentos)
    consulta = vectorizar(pregunta, vocabulario_)
    filas = []
    for fila, puntuacion in lexical:
        cos = coseno(consulta, vectorizar(f"{fila.get('heading', '')} {fila.get('content', '')}", vocabulario_))
        combinada = alfa * (puntuacion / maximo) + (1 - alfa) * cos
        if combinada >= umbral:
            filas.append((fila, combinada))
    return _top(filas)


def recuperar(
    modo: str,
    pregunta: str,
    fragmentos: list[dict[str, Any]],
    *,
    vectores: dict[str, list[float]] | None = None,
) -> list[dict[str, Any]]:
    if modo == "lexica":
        return recuperar_lexica(pregunta, fragmentos)
    if modo == "embeddings-locales":
        return recuperar_embeddings(pregunta, fragmentos)
    if modo == "embeddings-openai":
        return recuperar_embeddings_reales(pregunta, fragmentos, vectores or {})
    if modo == "hibrida":
        return recuperar_hibrida(pregunta, fragmentos)
    raise ValueError(f"Modo desconocido: {modo}")
