"""Contrato de citas literales; compartido con el servicio de dominio."""
import json
from typing import Any


def validar_seleccion(bruto: str, evidencia: list[dict[str, Any]]) -> str:
    """Acepta solo citas literales de fragmentos realmente enviados al modelo.

    El modelo selecciona evidencia; no se publica prosa libre con referencias
    añadidas. Esto verifica identidad textual y procedencia, no relevancia ni
    suficiencia de la recuperación: ambas requieren evaluación independiente.
    """
    salida = json.loads(bruto)
    if not isinstance(salida, dict) or set(salida) != {"evidence"}:
        raise ValueError("Esquema de selección inválido")
    filas = salida["evidence"]
    if not isinstance(filas, list) or not 1 <= len(filas) <= 6:
        raise ValueError("Sin selección verificable")
    permitidos = {fila["chunk_id"]: fila for fila in evidencia}
    resultado = []
    for fila in filas:
        if not isinstance(fila, dict) or set(fila) != {"chunk_id", "quote"}:
            raise ValueError("Cita sin estructura")
        chunk_id, quote = fila["chunk_id"], fila["quote"]
        if not isinstance(chunk_id, str) or chunk_id not in permitidos:
            raise ValueError("Fragmento no recuperado")
        if not isinstance(quote, str) or not 12 <= len(quote) <= 1200:
            raise ValueError("Cita fuera de límites")
        quote = " ".join(quote.split())
        content = " ".join(str(permitidos[chunk_id]["content"]).split())
        if len(quote) < 12 or quote not in content:
            raise ValueError("La afirmación no es una cita literal del fragmento")
        resultado.append(f"{quote} [{chunk_id}]")
    return "\n\n".join(dict.fromkeys(resultado))
