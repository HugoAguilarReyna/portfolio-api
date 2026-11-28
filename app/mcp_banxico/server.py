"""Piloto MCP local de recuperación de Banxico.

Esta herramienta **no** se expone por HTTP: el servidor se ejecuta por stdio y
solo lo abre un cliente MCP local. Reutiliza la recuperación léxica real de
`BriefingBanxico`, sin embeddings, sin LLM y sin escrituras.

El contrato es deliberadamente pequeño: una pregunta acotada y un máximo de tres
fragmentos. La respuesta no incluye contenido completo del corpus, rutas del
sistema, credenciales ni el documento entero; solo los campos que un consumidor
necesita para citar la evidencia.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app.config import Settings
from app.rag_banxico.service import BriefingBanxico, CorpusError

MAX_CONSULTA = 500
MIN_CONSULTA = 3
MAX_FRAGMENTOS = 3
MAX_EXTRACTO = 1200

mcp = FastMCP(
    "banxico-evidence",
    instructions=(
        "Servidor MCP de solo lectura para buscar fragmentos del corpus Banxico. "
        "No usa embeddings ni modelos; devuelve como máximo tres fragmentos."
    ),
)


def _servicio() -> BriefingBanxico:
    # Sin base: la recuperación usa el corpus versionado. No se abre Atlas ni
    # se configura LLM para esta herramienta.
    return BriefingBanxico(None, Settings(_env_file=None))


def _validar(consulta: str, limite: int) -> tuple[str, int]:
    if not isinstance(consulta, str):
        raise ValueError("«consulta» debe ser texto.")
    consulta = " ".join(consulta.split())
    if len(consulta) < MIN_CONSULTA:
        raise ValueError(f"«consulta» debe tener al menos {MIN_CONSULTA} caracteres.")
    if len(consulta) > MAX_CONSULTA:
        raise ValueError(f"«consulta» no puede superar {MAX_CONSULTA} caracteres.")
    if isinstance(limite, bool) or not isinstance(limite, int):
        raise ValueError("«limite» debe ser un entero.")
    if not 1 <= limite <= MAX_FRAGMENTOS:
        raise ValueError(f"«limite» debe estar entre 1 y {MAX_FRAGMENTOS}.")
    return consulta, limite


def buscar_fragmentos(consulta: str, limite: int = MAX_FRAGMENTOS) -> dict[str, Any]:
    """Busca evidencia Banxico con la recuperación léxica real y la acota."""
    consulta, limite = _validar(consulta, limite)
    try:
        servicio = _servicio()
        evidencia, modo = servicio.recuperar(consulta)
    except CorpusError as exc:
        raise ValueError("El corpus de Banxico no está disponible.") from exc

    fragmentos = []
    for fila in evidencia[:limite]:
        fuente = fila.get("source") or {}
        fragmentos.append(
            {
                "chunk_id": str(fila.get("chunk_id") or ""),
                "source_id": str(fila.get("source_id") or ""),
                "titulo": str(fuente.get("title") or fila.get("heading") or ""),
                "fecha": fuente.get("date"),
                "extracto": str(fila.get("content") or "")[:MAX_EXTRACTO],
                "score": fila.get("score"),
            }
        )

    return {
        "abstenido": not fragmentos,
        "fragmentos": fragmentos,
        "consulta": consulta,
        "limite_aplicado": limite,
        "modo_recuperacion": modo,
        "solo_lectura": True,
        "fuente": "corpus Banxico versionado",
    }


@mcp.tool(
    name="banxico_buscar_fragmentos",
    description=(
        "Busca hasta tres fragmentos del corpus Banxico con recuperación léxica. "
        "No usa embeddings, LLM ni escrituras; devuelve extractos acotados."
    ),
)
def banxico_buscar_fragmentos(consulta: str, limite: int = MAX_FRAGMENTOS) -> dict[str, Any]:
    """Herramienta MCP de solo lectura; la lógica real vive en `buscar_fragmentos`."""
    return buscar_fragmentos(consulta, limite)


def main() -> None:
    # stdio únicamente: no hay host/puerto público en este piloto.
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
