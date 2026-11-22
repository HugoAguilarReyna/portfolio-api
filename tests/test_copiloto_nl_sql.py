"""Pruebas del copiloto NL-to-SQL sin depender de Atlas ni de un modelo real."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.llm import cliente
from app.config import BACKEND_DIR, Settings
from app.nl_sql import service as nl_service
from app.nl_sql.service import CopilotoNlSql, QuestionError


class FakeDatabase:
    """Base mínima: el copiloto debe funcionar aunque Atlas no exista."""

    ping = False
    client = None


class FakeCollection:
    def __init__(self) -> None:
        self.documentos: list[dict] = []

    def insert_one(self, documento: dict) -> None:
        self.documentos.append(documento)


class FakeClient:
    def __init__(self, coleccion: FakeCollection) -> None:
        self.coleccion = coleccion

    def __getitem__(self, _base: str):
        return {"nl_sql_query_audit": self.coleccion}


class FakeAtlas:
    ping = True

    def __init__(self) -> None:
        self.coleccion = FakeCollection()
        self.client = FakeClient(self.coleccion)


def servicio(tmp_path, db=None, **overrides) -> CopilotoNlSql:
    valores = {
        "nl_sql_data_path": str(
            BACKEND_DIR / "data" / "snapshots" / "copiloto" / "data.json"
        ),
        "nl_sql_db_path": str(tmp_path / "wdi.sqlite3"),
        "nl_sql_audit_enabled": True,
        "llm_base_url": None,
        "llm_api_key": None,
        "llm_model": None,
    }
    valores.update(overrides)
    return CopilotoNlSql(db or FakeDatabase(), Settings(_env_file=None, **valores))


def test_snapshot_completo_sin_faltantes(tmp_path) -> None:
    servicio_local = servicio(tmp_path)
    assert len(servicio_local.rows) == 21
    assert servicio_local.data["period_start"] == 2019
    assert servicio_local.data["period_end"] == 2025
    assert {fila["indicator_code"] for fila in servicio_local.rows} == set(nl_service.METRICS)
    for codigo in nl_service.METRICS:
        assert len([fila for fila in servicio_local.rows if fila["indicator_code"] == codigo]) == 7


def test_ranking_de_inflacion_devuelve_serie_completa(tmp_path) -> None:
    resultado = servicio(tmp_path).consultar(
        "¿En qué año fue mayor la inflación entre 2019 y 2025?"
    )
    assert resultado["intent_mode"] == "reglas-locales"
    assert resultado["spec"]["kind"] == "ranking"
    assert resultado["spec"]["rank_order"] == "DESC"
    assert resultado["row_count"] == 7
    assert len(resultado["rows"]) == 7
    assert resultado["rows"][0]["year"] == 2022
    assert resultado["rows"][0]["value"] == pytest.approx(7.8962761917)
    assert resultado["sql"].count("?") == 4
    assert resultado["parameters"] == ["MEX", "FP.CPI.TOTL.ZG", 2019, 2025]
    assert "ORDER BY value DESC, year ASC" in resultado["sql"]
    assert "LIMIT" not in resultado["sql"].upper()
    assert "DROP" not in resultado["sql"].upper()


def test_comparacion_y_un_solo_anio(tmp_path) -> None:
    resultado = servicio(tmp_path).consultar("Compara PIB e inflación durante 2020")
    assert resultado["spec"]["kind"] == "comparison"
    assert resultado["spec"]["start_year"] == resultado["spec"]["end_year"] == 2020
    assert resultado["row_count"] == 2
    assert {fila["indicator_code"] for fila in resultado["rows"]} == {
        "NY.GDP.MKTP.KD.ZG",
        "FP.CPI.TOTL.ZG",
    }
    assert "2020" in resultado["summary"]


def test_ranking_negativo_del_pib(tmp_path) -> None:
    resultado = servicio(tmp_path).consultar(
        "¿En qué año fue menor el crecimiento del PIB entre 2019 y 2025?"
    )
    assert resultado["spec"]["kind"] == "ranking"
    assert resultado["spec"]["rank_order"] == "ASC"
    assert resultado["row_count"] == 7
    assert resultado["rows"][0]["year"] == 2020
    assert resultado["rows"][0]["value"] == pytest.approx(-8.4)
    assert "ORDER BY value ASC, year DESC" in resultado["sql"]


def test_errores_de_parser(tmp_path) -> None:
    servicio_local = servicio(tmp_path)
    with pytest.raises(QuestionError, match="solo cubre México"):
        servicio_local.parse_local("¿Cómo cambió el PIB de Guatemala?")
    with pytest.raises(QuestionError, match="No reconocí"):
        servicio_local.parse_local("¿Cuál es la capital de Francia?")
    with pytest.raises(QuestionError, match="2019–2025"):
        servicio_local.parse_local("¿Cómo cambió el PIB entre 2018 y 2025?")
    with pytest.raises(QuestionError, match="un solo indicador"):
        servicio_local.parse_local("¿Cuál fue el máximo de PIB e inflación en 2020?")


def test_texto_libre_no_se_convierte_en_sql(tmp_path) -> None:
    servicio_local = servicio(tmp_path)
    resultado = servicio_local.consultar("PIB 2020; DROP TABLE observations; --")
    assert resultado["row_count"] == 1
    assert "DROP" not in resultado["sql"].upper()
    assert resultado["parameters"][-2:] == [2020, 2020]
    # La tabla sigue disponible después de la pregunta con SQL aparente.
    assert servicio_local.ejecutar_lectura(
        "SELECT COUNT(*) AS n FROM observations", []
    )[0]["n"] == 21


def test_conexion_rechaza_escrituras(tmp_path) -> None:
    servicio_local = servicio(tmp_path)
    with pytest.raises(QuestionError, match="SELECT"):
        servicio_local.ejecutar_lectura("DELETE FROM observations", [])
    with sqlite3.connect(servicio_local.db_path) as con:
        assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 21


def test_plan_llm_valido_se_limita_a_json_y_plantilla(monkeypatch, tmp_path) -> None:
    class Respuesta:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "country_code": "MEX",
                                        "indicator_codes": ["NY.GDP.MKTP.KD.ZG"],
                                        "start_year": 2019,
                                        "end_year": 2025,
                                        "operation": "trend",
                                    }
                                )
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr(
        cliente,
        "post_chat",
        lambda settings, payload, *, timeout=None: json.loads(Respuesta().read()),
    )
    servicio_llm = servicio(
        tmp_path,
        llm_base_url="http://proveedor.invalid/v1",
        llm_api_key="clave-de-prueba",
        llm_model="modelo-de-prueba",
    )
    resultado = servicio_llm.consultar("Evolución del PIB de México")
    assert resultado["intent_mode"] == "llm-json-validado"
    assert resultado["spec"]["indicator_codes"] == ["NY.GDP.MKTP.KD.ZG"]
    assert "SELECT" in resultado["sql"]
    assert "PIB" not in resultado["sql"]


def test_plan_llm_invalido_cae_al_parser_local(monkeypatch, tmp_path) -> None:
    class Respuesta:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "country_code": "MEX",
                                        "indicator_codes": ["CODIGO.INVENTADO"],
                                        "start_year": 2019,
                                        "end_year": 2025,
                                        "operation": "trend",
                                    }
                                )
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr(
        cliente,
        "post_chat",
        lambda settings, payload, *, timeout=None: json.loads(Respuesta().read()),
    )
    resultado = servicio(
        tmp_path,
        llm_base_url="http://proveedor.invalid/v1",
        llm_api_key="clave-de-prueba",
        llm_model="modelo-de-prueba",
    ).consultar("¿Cómo cambió el PIB entre 2019 y 2025?")
    assert resultado["intent_mode"] == "reglas-locales"
    assert resultado["spec"]["indicator_codes"] == ["NY.GDP.MKTP.KD.ZG"]


def test_auditoria_no_guarda_la_pregunta(tmp_path) -> None:
    atlas = FakeAtlas()
    servicio_local = servicio(tmp_path, db=atlas)
    resultado = servicio_local.consultar("¿Cómo cambió el PIB entre 2019 y 2025?")
    assert resultado["audit"] == "atlas"
    assert len(atlas.coleccion.documentos) == 1
    documento = atlas.coleccion.documentos[0]
    assert "question" not in documento
    assert "pregunta" not in documento
    serializado = json.dumps(documento, default=str, ensure_ascii=False)
    assert "cambió" not in serializado
    assert documento["indicators"] == ["NY.GDP.MKTP.KD.ZG"]


def test_salud_no_expone_uri_ni_claves(tmp_path) -> None:
    salud = servicio(tmp_path).salud()
    serializado = json.dumps(salud, ensure_ascii=False)
    assert salud["observations"] == 21
    assert salud["sqlite"]["read_only"] is True
    assert salud["parser"] == "lexico-plantillas"
    assert salud["atlas"]["collection"] == "nl_sql_query_audit"
    assert "mongodb://" not in serializado
    assert "mongodb+srv://" not in serializado
    assert "api_key" not in serializado.lower()
