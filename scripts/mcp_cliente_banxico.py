"""Cliente MCP local que descubre e invoca la búsqueda de Banxico.

No usa la API HTTP del portafolio ni un cliente simulado: arranca el servidor MCP
por stdio, lee el catálogo de herramientas y ejecuta la recuperación léxica real
del corpus Banxico. Sirve como consumidor de demostración mientras «Analista
asistido por IA» siga siendo un caso de roadmap.

    python scripts/mcp_cliente_banxico.py
    python scripts/mcp_cliente_banxico.py --salida docs/evidencia-mcp.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

RAIZ = Path(__file__).resolve().parents[1]
SERVIDOR = "banxico_buscar_fragmentos"


def _texto(resultado: Any) -> dict[str, Any]:
    """Extrae el objeto JSON que devuelve FastMCP, sin imprimir todo el protocolo."""
    if getattr(resultado, "isError", False):
        raise RuntimeError("La herramienta MCP devolvió un error.")
    estructurado = getattr(resultado, "structuredContent", None)
    if isinstance(estructurado, dict):
        return estructurado
    for bloque in getattr(resultado, "content", []) or []:
        texto = getattr(bloque, "text", None)
        if texto:
            return json.loads(texto)
    raise RuntimeError("La herramienta MCP no devolvió contenido JSON.")


async def ejecutar() -> dict[str, Any]:
    entorno = dict(os.environ)
    entorno["PYTHONPATH"] = str(RAIZ) + (os.pathsep + entorno["PYTHONPATH"] if entorno.get("PYTHONPATH") else "")
    parametros = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_banxico.server"],
        cwd=str(RAIZ),
        env=entorno,
    )
    async with stdio_client(parametros) as (lectura, escritura):
        async with ClientSession(lectura, escritura) as sesion:
            await sesion.initialize()
            catalogo = await sesion.list_tools()
            herramienta = next((t for t in catalogo.tools if t.name == SERVIDOR), None)
            if herramienta is None:
                raise RuntimeError(f"No se descubrió la herramienta {SERVIDOR}.")
            consulta = await sesion.call_tool(
                SERVIDOR,
                {"consulta": "¿cuál es la tasa objetivo de Banco de México?", "limite": 2},
            )
            abstenida = await sesion.call_tool(
                SERVIDOR,
                {"consulta": "zzzz qqqq xxxx", "limite": 1},
            )
            return {
                "transporte": "stdio",
                "servidor": "banxico-evidence",
                "descubrimiento": {
                    "herramientas": [
                        {
                            "nombre": t.name,
                            "descripcion": t.description,
                            "schema": t.inputSchema,
                        }
                        for t in catalogo.tools
                    ],
                },
                "invocacion": {
                    "herramienta": SERVIDOR,
                    "consulta": "¿cuál es la tasa objetivo de Banco de México?",
                    "resultado": _texto(consulta),
                },
                "abstencion": {
                    "consulta": "zzzz qqqq xxxx",
                    "resultado": _texto(abstenida),
                },
                "solo_lectura": True,
                "servidor_expuesto_publicamente": False,
            }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cliente MCP local de Banxico.")
    parser.add_argument("--salida", type=Path, default=None)
    args = parser.parse_args(argv)
    datos = anyio.run(ejecutar)
    texto = json.dumps(datos, ensure_ascii=False, indent=2)
    if args.salida:
        args.salida.parent.mkdir(parents=True, exist_ok=True)
        args.salida.write_text(texto + "\n", encoding="utf-8")
    print(texto)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
