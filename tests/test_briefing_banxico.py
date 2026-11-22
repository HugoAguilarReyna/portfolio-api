"""Pruebas del servicio de «Briefing con evidencia» sin depender de Atlas."""

from __future__ import annotations

import json
import pytest

from app.config import BACKEND_DIR, Settings
from app.llm import cliente
from app.rag_banxico.service import BriefingBanxico, puntuar


class FakeDatabase:
    """Base mínima: la demo debe funcionar aunque Atlas no exista."""

    ping = False
    client = None


def servicio(**overrides) -> BriefingBanxico:
    valores = {
        "rag_banxico_corpus_path": str(
            BACKEND_DIR / "data" / "snapshots" / "banxico" / "corpus.json"
        ),
        "llm_base_url": None,
        "llm_api_key": None,
        "llm_model": None,
    }
    valores.update(overrides)
    return BriefingBanxico(FakeDatabase(), Settings(_env_file=None, **valores))


def test_recupera_tasa_y_cita_fuente() -> None:
    resultado = servicio().consultar("¿Cuál fue la decisión sobre la tasa y desde cuándo aplica?")
    assert resultado["retrieval_mode"] == "corpus-local"
    assert resultado["mode"] == "extractiva"
    assert resultado["vector_search"] is False
    assert resultado["evidence"]
    assert any(item["chunk_id"] == "BM-2026-11-05-TASA" for item in resultado["evidence"])
    assert "BM-2026-11-05" in resultado["answer"] or resultado["evidence"][0]["source_id"] == "BM-2026-11-05"


def test_no_inventa_evidencia() -> None:
    resultado = servicio().consultar("¿Cuál es la capital de Francia?")
    assert resultado["mode"] == "sin-coincidencias"
    assert resultado["evidence"] == []
    assert "No encontré evidencia suficiente" in resultado["answer"]


def test_salud_no_expone_uri_ni_claves() -> None:
    salud = servicio().salud()
    serializado = json.dumps(salud, ensure_ascii=False)
    assert salud["corpus_chunks"] == 7
    assert salud["vector_search"] is False
    assert salud["mongo"]["target"] in {"local", "atlas"}
    assert "mongodb://" not in serializado
    assert "mongodb+srv://" not in serializado
    assert "api_key" not in serializado.lower()


def test_puntuacion_solo_devuelve_coincidencias() -> None:
    servicio_local = servicio()
    assert puntuar("riesgos al alza", servicio_local.corpus["chunks"])[0]["chunk_id"] == "BM-2026-11-05-RIESGOS"
    assert puntuar("capital de Francia", servicio_local.corpus["chunks"]) == []


def test_modelo_configurado_responde_solo_con_evidencia(monkeypatch) -> None:
    """El camino LLM usa solo la evidencia y exige citarla."""
    capturado = {}

    def falso_post_chat(settings, payload, *, timeout=None):
        capturado["payload"] = payload
        return {"choices": [{"message": {"content": seleccion_valida()}}]}

    monkeypatch.setattr(cliente, "post_chat", falso_post_chat)
    servicio_llm = servicio(
        llm_base_url="http://proveedor.invalid/v1",
        llm_api_key="clave-de-prueba",
        llm_model="modelo-de-prueba",
    )
    resultado = servicio_llm.consultar("¿Cuál fue la decisión sobre la tasa?")

    assert resultado["mode"] == "llm-grounded"
    assert "6.25%" in resultado["answer"]
    assert "[BM-2026-11-05-TASA]" in resultado["answer"]
    assert resultado["vector_search"] is False
    # Solo viajan la pregunta y los fragmentos recuperados.
    mensajes = capturado["payload"]["messages"]
    assert len(mensajes) == 2
    assert "Evidencia recuperada:" in mensajes[1]["content"]
    assert "corpus" not in mensajes[1]["content"].lower()
    assert "clave-de-prueba" not in json.dumps(capturado["payload"])
    assert capturado["payload"]["max_tokens"] > 0


def test_modelo_que_falla_conserva_respuesta_extractiva(monkeypatch) -> None:
    """Un error del proveedor no deja la consulta sin respuesta ni la inventa."""

    def fallar(settings, payload, *, timeout=None):
        raise cliente.LlmUpstreamError("proveedor no disponible")

    monkeypatch.setattr(cliente, "post_chat", fallar)
    servicio_llm = servicio(
        llm_base_url="http://proveedor.invalid/v1",
        llm_api_key="clave-de-prueba",
        llm_model="modelo-de-prueba",
    )
    resultado = servicio_llm.consultar("¿Cuál fue la decisión sobre la tasa?")
    assert resultado["mode"] == "extractiva-respaldo"
    assert resultado["evidence"]
    assert "6.25%" in resultado["answer"]


def test_cita_inventada_cae_a_extractiva(monkeypatch) -> None:
    """Una cita que no existe en el corpus invalida la respuesta del modelo."""

    def inventar(settings, payload, *, timeout=None):
        return {
            "choices": [
                {
                    "message": {
                        "content": "La tasa subió [BM-1999-01-01-INVENTADO] según el boletín."
                    }
                }
            ]
        }

    monkeypatch.setattr(cliente, "post_chat", inventar)
    servicio_llm = servicio(
        llm_base_url="http://proveedor.invalid/v1",
        llm_api_key="clave-de-prueba",
        llm_model="modelo-de-prueba",
    )
    resultado = servicio_llm.consultar("¿Cuál fue la decisión sobre la tasa?")
    assert resultado["mode"] == "extractiva-respaldo"
    assert "INVENTADO" not in resultado["answer"]


def test_sin_configuracion_no_se_llama_al_proveedor(monkeypatch) -> None:
    def prohibido(*args, **kwargs):
        raise AssertionError("no debe llamarse al proveedor sin configuración")

    monkeypatch.setattr(cliente, "post_chat", prohibido)
    resultado = servicio().consultar("¿Cuál fue la decisión sobre la tasa?")
    assert resultado["mode"] == "extractiva"
    assert resultado["evidence"]


def test_el_limite_de_consultas_degrada_a_extractiva(monkeypatch) -> None:
    cliente.VENTANA.reiniciar()

    def prohibido(*args, **kwargs):
        raise AssertionError("la ventana debía impedir la llamada")

    monkeypatch.setattr(cliente, "post_chat", prohibido)
    servicio_llm = servicio(
        llm_base_url="http://proveedor.invalid/v1",
        llm_api_key="clave-de-prueba",
        llm_model="modelo-de-prueba",
        llm_consultas_por_minuto=1,
    )
    # La primera consulta consume el cupo; la segunda no llega al proveedor.
    monkeypatch.setattr(
        cliente,
        "post_chat",
        lambda *a, **k: {"choices": [{"message": {"content": seleccion_valida()}}]},
    )
    primera = servicio_llm.consultar("¿Cuál fue la decisión sobre la tasa?")
    assert primera["mode"] == "llm-grounded"

    monkeypatch.setattr(cliente, "post_chat", prohibido)
    segunda = servicio_llm.consultar("¿Cuál fue la decisión sobre la tasa?")
    assert segunda["mode"] == "extractiva-respaldo"
    cliente.VENTANA.reiniciar()

def seleccion_valida():
    chunk = servicio().corpus["chunks"][0]
    return json.dumps({"evidence": [{"chunk_id": chunk["chunk_id"], "quote": chunk["content"]}]})


@pytest.mark.parametrize("bruto", [
    "La tasa ahora es 99%.",
    '{"evidence":[]}',
    '{"evidence":[{"chunk_id":"BM-2026-11-05-TASA","quote":"La tasa ahora es 99%."}]}',
    '{"evidence":[{"chunk_id":"BM-2026-08-26-ACTIVIDAD","quote":"Texto de una fuente no recuperada"}]}',
    '{"evidence":[{"chunk_id":"BM-2026-11-05-TASA","quote":""}]}',
    '{"evidence":null}',
    '{"evidence":[{"chunk_id":[],"quote":"Una cita con identificador inválido"}]}',
])
def test_sin_soporte_no_se_etiqueta_grounded(monkeypatch, bruto):
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: {"choices": [{"message": {"content": bruto}}]})
    resultado = servicio(llm_base_url="http://proveedor.invalid/v1", llm_api_key="prueba", llm_model="prueba").consultar("decisión tasa")
    assert resultado["mode"] == "extractiva-respaldo"
    assert "99%" not in resultado["answer"]


def test_fuente_real_fuera_de_recuperacion_no_se_acepta():
    from app.rag_banxico.validacion import validar_seleccion
    chunks = servicio().corpus["chunks"]
    bruto = json.dumps({"evidence": [{"chunk_id": chunks[1]["chunk_id"], "quote": chunks[1]["content"]}]})
    with pytest.raises(ValueError, match="no recuperado"):
        validar_seleccion(bruto, [chunks[0]])
