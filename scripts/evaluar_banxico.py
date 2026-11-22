"""Evalúa la recuperación y las respuestas del briefing de Banxico.

Modo simulado (sin red, sin coste):

    python scripts/evaluar_banxico.py

Compara recuperación léxica, por embeddings locales e híbrida; mide recall@3 y
MRR, y evalúa citas y abstención con la respuesta extractiva y con un proveedor
simulado. Los vectores reales de OpenAI (`embeddings-openai`) solo se usan si
existe el JSON versionado; generarlos cuesta y lo hace otro comando.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app.config import Settings  # noqa: E402
from app.evals import banxico as evals  # noqa: E402
from app.llm import cliente  # noqa: E402
from app.rag_banxico import embeddings  # noqa: E402
from app.rag_banxico.service import BriefingBanxico, CorpusError  # noqa: E402

MODOS = ("lexica", "embeddings-locales", "hibrida")


class DbFalsa:
    ping = False
    client = None


def _servicio(**extra) -> BriefingBanxico:
    valores = {
        "llm_base_url": None,
        "llm_api_key": None,
        "llm_model": None,
        "llm_consultas_por_minuto": 120,
    }
    valores.update(extra)
    return BriefingBanxico(DbFalsa(), Settings(_env_file=None, **valores))


def evaluar_recuperacion(preguntas: list[dict[str, Any]], fragmentos: list[dict[str, Any]]) -> dict[str, Any]:
    salida: dict[str, Any] = {}
    for modo in MODOS:
        filas = []
        for pregunta in preguntas:
            recuperados = [fila["chunk_id"] for fila in embeddings.recuperar(modo, pregunta["pregunta"], fragmentos)]
            filas.append(
                {
                    "id": pregunta["id"],
                    "categoria": pregunta["categoria"],
                    "relevantes": pregunta["relevantes"],
                    "recuperados": recuperados,
                    "recall@3": evals.recall_en_k(recuperados, pregunta["relevantes"]),
                    "mrr": evals.reciproco_ranking(recuperados, pregunta["relevantes"]),
                }
            )
        salida[modo] = {
            "metricas": evals.agregar(filas),
            "fallos_recall": [
                fila["id"] for fila in filas if fila["recall@3"] == 0.0
            ],
        }
    return salida


def evaluar_respuestas(preguntas: list[dict[str, Any]], servicio: BriefingBanxico) -> dict[str, Any]:
    """Citas verificables y abstención sin respaldo, con extractiva y con simulado."""
    filas = []
    for pregunta in preguntas:
        evidencia, _modo_recuperacion = servicio.recuperar(pregunta["pregunta"])
        respuesta, modo_respuesta = servicio.responder(pregunta["pregunta"], evidencia)
        ids = {str(fila["chunk_id"]) for fila in evidencia}
        citadas = {
            cita
            for cita in __import__("re").findall(r"\[(BM-[A-Za-z0-9-]+)\]", respuesta)
        }
        filas.append(
            {
                "id": pregunta["id"],
                "categoria": pregunta["categoria"],
                "sin_respaldo": not pregunta["relevantes"],
                "modo_respuesta": modo_respuesta,
                "evidencias": len(evidencia),
                "citas": sorted(citadas),
                "citas_en_evidencia": citadas <= ids if citadas else True,
                "abstiene": modo_respuesta == "sin-coincidencias",
            }
        )

    sin_respaldo = [fila for fila in filas if fila["sin_respaldo"]]
    con_respaldo = [fila for fila in filas if not fila["sin_respaldo"]]
    return {
        "extractiva": {
            "respuestas_con_evidencia": len(con_respaldo),
            "citas_en_evidencia": all(fila["citas_en_evidencia"] for fila in filas),
            "abstencion_en_sin_respaldo": round(
                sum(1 for fila in sin_respaldo if fila["abstiene"]) / len(sin_respaldo), 4
            )
            if sin_respaldo
            else None,
            "modos": sorted({fila["modo_respuesta"] for fila in filas}),
        },
        "citas_inventadas_bloqueadas": None,
        "proveedor_simulado": None,
    }


def evaluar_citas_simuladas(_servicio_base: BriefingBanxico) -> dict[str, Any]:
    """Con proveedor simulado: cita válida y cita inventada (debe degradar)."""
    servicio = _servicio(
        llm_base_url="https://proveedor-simulado.invalid/v1",
        llm_api_key="clave-ficticia-de-evaluacion",
        llm_model="mock-citas-v1",
    )
    original = cliente.post_chat
    resultados: dict[str, Any] = {}

    def respuesta_con(contenido: str):
        def falso(settings, payload, *, timeout=None):
            return {"choices": [{"message": {"content": contenido}}]}

        return falso

    try:
        pregunta = "¿A cuánto quedó la tasa objetivo?"
        evidencia = servicio.recuperar(pregunta)[0]
        primera = evidencia[0]["chunk_id"]
        seleccion = json.dumps({"evidence": [{"chunk_id": primera, "quote": evidencia[0]["content"]}]})
        cliente.post_chat = respuesta_con(seleccion)
        texto, modo = servicio.responder(pregunta, evidencia)
        resultados["cita_valida"] = {"modo": modo, "menciona_fuente": primera in texto}

        cliente.post_chat = respuesta_con("La tasa subió [BM-1999-01-01-INVENTADA].")
        texto, modo = servicio.responder(pregunta, evidencia)
        resultados["cita_inventada"] = {"modo": modo, "cita_en_evidencia": False}
    finally:
        cliente.post_chat = original
    return resultados


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evalúa recuperación y respuestas de Banxico.")
    parser.add_argument("--salida", type=Path, default=None)
    args = parser.parse_args(argv)

    preguntas = evals.cargar_preguntas()
    servicio = _servicio()
    fragmentos = servicio.corpus["chunks"]
    vectores = embeddings.cargar_vectores(evals.VECTORES)

    recuperacion = evaluar_recuperacion(preguntas, fragmentos)
    respuestas = evaluar_respuestas(preguntas, servicio)
    respuestas["proveedor_simulado"] = evaluar_citas_simuladas(servicio)
    respuestas["citas_inventadas_bloqueadas"] = (
        respuestas["proveedor_simulado"]["cita_inventada"]["modo"] == "extractiva-respaldo"
    )

    resultado = {
        "conjunto": {
            "version": evals.VERSION,
            "ruta": "app/evals/banxico/preguntas.jsonl",
            "preguntas": len(preguntas),
            "sintetico": True,
        },
        "corpus": {
            "chunks": len(fragmentos),
            "fuentes": sorted({fila["source_id"] for fila in fragmentos}),
            "ampliado_en_esta_fase": False,
            "motivo": (
                "no se añadieron publicaciones nuevas: no es posible verificar en línea "
                "la procedencia de nuevos documentos oficiales desde este entorno"
            ),
        },
        "ejecucion": {
            "fecha": datetime.now(timezone.utc).isoformat(),
            "modo": "simulado",
            "grounding_contract": "verbatim-retrieved-chunks",
            "proveedor_real": False,
            "modelo": "sin-modelo (extractiva + simulado)",
            "vectores_reales": bool(vectores),
        },
        "recuperacion": recuperacion,
        "respuestas": respuestas,
        "costo": {
            "usd": "no calculable",
            "motivo": "no se hizo ninguna llamada al proveedor; no hay precios verificados en el repositorio",
        },
        "notas": [
            "Conjunto sintético de 32 preguntas; los fragmentos relevantes están marcados a mano.",
            "`embeddings-locales` es una bolsa de términos ponderada con NumPy, no un modelo de embeddings.",
            "`embeddings-openai` requiere generar y versionar vectores con el comando explícito; no se ejecutó.",
            "La recuperación léxica sigue siendo el baseline y el fallback.",
        ],
    }
    destino = args.salida or (
        evals.RESULTADOS
        / f"banxico-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-simulado-v{evals.VERSION.replace('.', '')}.json"
    )
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")

    for modo, datos in recuperacion.items():
        metricas = datos["metricas"]
        print(
            f"{modo:22} recall@3={metricas['recall@3']} mrr={metricas['mrr']} "
            f"abstencion={metricas['abstencion_correcta']}"
        )
    print(f"citas inventadas bloqueadas: {respuestas['citas_inventadas_bloqueadas']}")
    print(f"resultados escritos en {destino}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
