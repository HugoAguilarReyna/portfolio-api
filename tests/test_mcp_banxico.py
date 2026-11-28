"""Pruebas del piloto MCP de Banxico (sin red y sin LLM)."""

from __future__ import annotations

import pytest

from app.mcp_banxico import server


def test_busqueda_acotada_y_con_campos_publicos() -> None:
    resultado = server.buscar_fragmentos("tasa objetivo", 2)
    assert resultado["solo_lectura"] is True
    assert resultado["abstenido"] is False
    assert 1 <= len(resultado["fragmentos"]) <= 2
    assert resultado["limite_aplicado"] == 2
    for fragmento in resultado["fragmentos"]:
        assert set(fragmento) >= {"chunk_id", "source_id", "titulo", "extracto", "score"}
        assert len(fragmento["extracto"]) <= server.MAX_EXTRACTO
        assert "content" not in fragmento


def test_sin_coincidencias_se_abstiene() -> None:
    resultado = server.buscar_fragmentos("zzzz qqqq xxxx", 1)
    assert resultado["abstenido"] is True
    assert resultado["fragmentos"] == []


@pytest.mark.parametrize(
    "consulta,limite",
    [
        ("", 1),
        ("ab", 1),
        ("x" * 501, 1),
        ("tasa objetivo", 0),
        ("tasa objetivo", 4),
        ("tasa objetivo", True),
    ],
)
def test_parametros_invalidos(consulta: str, limite: int) -> None:
    with pytest.raises(ValueError):
        server.buscar_fragmentos(consulta, limite)


def test_herramienta_mcp_delega_en_la_logica_real() -> None:
    resultado = server.banxico_buscar_fragmentos("inflación", 1)
    assert resultado["fragmentos"]
    assert len(resultado["fragmentos"]) == 1
