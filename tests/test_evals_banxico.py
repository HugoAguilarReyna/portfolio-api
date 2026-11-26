"""Pruebas de la evaluación del briefing de Banxico (sin red ni proveedor real)."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path

import pytest

from app.config import Settings
from app.evals import banxico as evals
from app.llm import cliente
from app.rag_banxico import embeddings
from app.rag_banxico.service import BriefingBanxico

RAIZ = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def ventana_llm_aislada():
    cliente.VENTANA.reiniciar()
    yield
    cliente.VENTANA.reiniciar()


class DbFalsa:
    ping = False
    client = None


def servicio(**extra) -> BriefingBanxico:
    valores = {"llm_base_url": None, "llm_api_key": None, "llm_model": None}
    valores.update(extra)
    return BriefingBanxico(DbFalsa(), Settings(_env_file=None, **valores))


def test_conjunto_de_preguntas() -> None:
    preguntas = evals.cargar_preguntas()
    assert 30 <= len(preguntas) <= 40
    corpus = servicio().corpus["chunks"]
    ids = {fila["chunk_id"] for fila in corpus}
    for pregunta in preguntas:
        assert set(pregunta["relevantes"]) <= ids
        if pregunta["categoria"] == "sin_respaldo":
            assert pregunta["relevantes"] == []


def test_corpus_ampliado_y_sin_secretos() -> None:
    corpus = servicio().corpus
    assert len(corpus["chunks"]) == 12
    assert {fila["source_id"] for fila in corpus["chunks"]} == {"BM-2026-11-05", "BM-2026-08-26"}
    ids = {fila["chunk_id"] for fila in corpus["chunks"]}
    assert "BM-2026-08-26-PRONOSTICO-ACTIVIDAD" in ids
    assert "BM-2026-08-26-NO-SUBYACENTE" in ids
    crudo = evals.PREGUNTAS.read_text(encoding="utf-8")
    for patron in ("sk-", "Bearer ", "github_pat_", "password"):
        assert patron not in crudo


def test_metricas_unitarias() -> None:
    assert evals.recall_en_k(["a", "b", "c"], ["c"]) == 1.0
    assert evals.recall_en_k(["a", "b", "c"], ["z"]) == 0.0
    assert evals.recall_en_k([], []) is None
    assert evals.reciproco_ranking(["a", "b"], ["b"]) == 0.5
    assert evals.reciproco_ranking(["a"], ["z"]) == 0.0
    metricas = evals.agregar(
        [
            {"relevantes": ["x"], "recuperados": ["x"], "recall@3": 1.0, "mrr": 1.0},
            {"relevantes": ["y"], "recuperados": [], "recall@3": 0.0, "mrr": 0.0},
            {"relevantes": [], "recuperados": [], "recall@3": None, "mrr": None},
        ]
    )
    assert metricas["recall@3"] == 0.5
    assert metricas["mrr"] == 0.5
    assert metricas["abstencion_correcta"] == 1.0


def test_modos_de_recuperacion_son_deterministas_y_cubren_el_corpus() -> None:
    base = servicio()
    preguntas = evals.cargar_preguntas()
    fragmentos = base.corpus["chunks"]
    for modo in ("lexica", "embeddings-locales", "hibrida"):
        primera = [embeddings.recuperar(modo, p["pregunta"], fragmentos) for p in preguntas]
        segunda = [embeddings.recuperar(modo, p["pregunta"], fragmentos) for p in preguntas]
        assert primera == segunda, modo
        filas = [
            {
                "relevantes": p["relevantes"],
                "recuperados": [f["chunk_id"] for f in recuperados],
                "recall@3": evals.recall_en_k([f["chunk_id"] for f in recuperados], p["relevantes"]),
                "mrr": evals.reciproco_ranking([f["chunk_id"] for f in recuperados], p["relevantes"]),
            }
            for p, recuperados in zip(preguntas, primera)
        ]
        metricas = evals.agregar(filas)
        assert metricas["recall@3"] == 1.0, modo


def test_todos_los_modos_se_abstienen_sin_coincidencias() -> None:
    fragmentos = servicio().corpus["chunks"]
    for modo in ("lexica", "embeddings-locales", "hibrida"):
        assert embeddings.recuperar(modo, "capital de Francia", fragmentos) == [], modo
        assert embeddings.recuperar(modo, "¿cuál es la tasa objetivo?", fragmentos), modo


def test_el_umbral_deja_fuera_las_cuatro_preguntas_sin_respaldo() -> None:
    fragmentos = servicio().corpus["chunks"]
    sin_respaldo = [p for p in evals.cargar_preguntas() if not p["relevantes"]]
    assert len(sin_respaldo) == 4
    for pregunta in sin_respaldo:
        for modo in ("lexica", "embeddings-locales", "hibrida"):
            assert embeddings.recuperar(modo, pregunta["pregunta"], fragmentos) == [], (
                pregunta["id"],
                modo,
            )


def test_citas_inventadas_degradan_a_extractiva(monkeypatch) -> None:
    original = cliente.post_chat
    configurado = servicio(
        llm_base_url="https://proveedor-simulado.invalid/v1",
        llm_api_key="clave-ficticia-de-evaluacion",
        llm_model="mock",
    )
    pregunta = "¿A cuánto quedó la tasa objetivo?"
    evidencia, _ = configurado.recuperar(pregunta)
    try:
        monkeypatch.setattr(
            cliente,
            "post_chat",
            lambda *a, **k: {"choices": [{"message": {"content": json.dumps({"evidence": [{"chunk_id": evidencia[0]["chunk_id"], "quote": evidencia[0]["content"]}]})}}]},
        )
        texto, modo = configurado.responder(pregunta, evidencia)
        assert modo == "llm-grounded"
        assert "BM-2026-11-05" in texto

        monkeypatch.setattr(
            cliente,
            "post_chat",
            lambda *a, **k: {"choices": [{"message": {"content": "Subió [BM-1999-01-01-INVENTADA]."}}]},
        )
        texto, modo = configurado.responder(pregunta, evidencia)
        assert modo == "extractiva-respaldo"
        assert "INVENTADA" not in texto
    finally:
        cliente.post_chat = original


def test_generacion_de_embeddings_exige_confirmacion(monkeypatch) -> None:
    ruta = RAIZ / "scripts" / "generar_embeddings_banxico.py"
    especificacion = importlib.util.spec_from_file_location("generar_embeddings", ruta)
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    monkeypatch.delenv("PORTFOLIO_EVAL_LIVE", raising=False)
    assert modulo.main(["--modelo", "text-embedding-3-small"]) == 2
