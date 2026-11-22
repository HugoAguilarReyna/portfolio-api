"""Genera vectores reales del corpus de Banxico con el proveedor de embeddings.

NO se ejecuta en las pruebas: hace llamadas reales y genera cargos.

    PORTFOLIO_EVAL_LIVE=1 python scripts/generar_embeddings_banxico.py --modelo text-embedding-3-small

Guarda `app/evals/banxico/vectores-banxico.json` con los vectores del corpus y de
las preguntas de evaluación. Antes de versionarlo revisa formato, tamaño y si
conviene publicarlo: son datos derivados del corpus, no secretos.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app.config import Settings  # noqa: E402
from app.evals import banxico as evals  # noqa: E402
from app.llm import cliente  # noqa: E402

RUTA = evals.VECTORES


def _embeddings(settings: Settings, modelo: str, textos: list[str]) -> list[list[float]]:
    if not cliente.configurado(settings):
        raise SystemExit("El proveedor no está configurado; no se generan embeddings.")
    import json as _json
    from urllib.request import Request, urlopen

    payload = {"model": modelo, "input": textos}
    peticion = Request(
        f"{str(settings.llm_base_url).rstrip('/')}/embeddings",
        data=_json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(peticion, timeout=float(settings.llm_timeout_s)) as respuesta:
        datos = _json.loads(respuesta.read())
    return [fila["embedding"] for fila in datos["data"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera embeddings reales del corpus de Banxico.")
    parser.add_argument("--modelo", default="text-embedding-3-small")
    args = parser.parse_args(argv)

    print(
        "AVISO: esto llama al proveedor de embeddings y PUEDE GENERAR CARGOS. "
        "No forma parte de la evaluación simulada.",
        file=sys.stderr,
    )
    if os.environ.get("PORTFOLIO_EVAL_LIVE") != "1":
        print("Bloqueado: define PORTFOLIO_EVAL_LIVE=1 para confirmar.", file=sys.stderr)
        return 2

    settings = Settings(_env_file=None)
    from app.rag_banxico.service import BriefingBanxico

    class DbFalsa:
        ping = False
        client = None

    servicio = BriefingBanxico(DbFalsa(), settings)
    fragmentos = servicio.corpus["chunks"]
    preguntas = evals.cargar_preguntas()
    textos = [f"{fila['heading']} {fila['content']}" for fila in fragmentos] + [
        pregunta["pregunta"] for pregunta in preguntas
    ]
    vectores = _embeddings(settings, args.modelo, textos)
    salida = {
        "modelo": args.modelo,
        "generado": datetime.now(timezone.utc).isoformat(),
        "dimension": len(vectores[0]) if vectores else 0,
        "vectores": {
            **{
                str(fila["chunk_id"]): vector
                for fila, vector in zip(fragmentos, vectores[: len(fragmentos)])
            },
            **{
                "__consulta__:" + pregunta["pregunta"]: vector
                for pregunta, vector in zip(preguntas, vectores[len(fragmentos) :])
            },
        },
    }
    RUTA.parent.mkdir(parents=True, exist_ok=True)
    RUTA.write_text(json.dumps(salida), encoding="utf-8")
    print(f"vectores escritos en {RUTA} ({len(salida['vectores'])} entradas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
