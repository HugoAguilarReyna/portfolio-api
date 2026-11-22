"""Pruebas del análisis consultivo de Kilómetro Cero, Ripple Room y DockMind.

Sin red y sin credenciales: el transporte del proveedor se sustituye por un
doble. Se cubre el camino válido, los rechazos por esquema y catálogo, la falta
de configuración, el error del proveedor y el límite de frecuencia.
"""

from __future__ import annotations

import json

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.analisis import dockmind, kilometro_cero, ripple_room
from app.api.v1.routes import dockmind as ruta_dockmind
from app.api.v1.routes import kilometro_cero as ruta_kilometro
from app.api.v1.routes import ripple_room as ruta_ripple
from app.api.deps import exigir_cuota_llm
from app.config import Settings, get_settings
from app.llm import cliente


def ajustes(**extra) -> Settings:
    base = {
        "_env_file": None,
        "llm_base_url": "https://proveedor.invalid/v1",
        "llm_api_key": "clave-ficticia-de-prueba",
        "llm_model": "modelo-de-prueba",
        "llm_consultas_por_minuto": 120,
    }
    base.update(extra)
    return Settings(**base)


SIN_LLM = {"llm_base_url": None, "llm_api_key": None, "llm_model": None}


def sobre(datos: dict) -> dict:
    return {
        "choices": [{"message": {"content": json.dumps(datos, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 60, "total_tokens": 160},
    }


@pytest.fixture(autouse=True)
def _ventana_limpia():
    cliente.VENTANA.reiniciar()
    yield
    cliente.VENTANA.reiniciar()


# --------------------------------------------------------------- Kilómetro Cero
def contexto_kilometro() -> dict:
    return {
        "escenario": {"id": "storm", "nombre": "Tormenta", "descripcion": "Lluvia intensa"},
        "red": {"envios": 128, "a_tiempo": 96, "retrasados": 22, "en_riesgo": 10, "corredores_activos": 2},
        "corredores": [
            {
                "id": "C-01",
                "origen": "CDMX",
                "destino": "Puebla",
                "envios": 24,
                "a_tiempo": 16,
                "retrasados": 6,
                "en_riesgo": 2,
                "km": 260.0,
            },
            {
                "id": "C-02",
                "origen": "Monterrey",
                "destino": "Saltillo",
                "envios": 18,
                "a_tiempo": 15,
                "retrasados": 2,
                "en_riesgo": 1,
                "km": 90.0,
            },
        ],
        "incidencias": [
            {"id": "INC-1", "tipo": "storm", "detalle": "Cierre parcial", "corredores_afectados": ["C-01"]}
        ],
        "acciones_disponibles": list(kilometro_cero.ACCIONES),
    }


def plan_kilometro(**extra) -> dict:
    datos = {
        "recomendacion": "desviar_por_corredor_alterno",
        "justificacion": "El corredor C-01 concentra los retrasos del escenario.",
        "evidencia": ["C-01 suma 6 envíos retrasados de 24."],
        "riesgos": ["El desvío alarga la distancia del corredor."],
        "corredores_sugeridos": ["C-01"],
        "acciones_alternas": ["priorizar_carga_critica"],
    }
    datos.update(extra)
    return datos


def test_kilometro_cero_camino_valido(monkeypatch) -> None:
    capturado = {}

    def falso(settings, payload, *, timeout=None):
        capturado["payload"] = payload
        return sobre(plan_kilometro())

    monkeypatch.setattr(cliente, "post_chat", falso)
    salida = kilometro_cero.analizar(ajustes(), contexto_kilometro())

    assert salida["modo"] == "openai"
    assert salida["recomendacion"]["accion"] == "desviar_por_corredor_alterno"
    assert salida["recomendacion"]["corredores_sugeridos"] == ["C-01"]
    assert salida["advisory_only"] is True
    assert salida["puede_despachar"] is False
    assert salida["datos_reales"] is False
    assert salida["aviso"] == "simulacion"
    assert salida["uso"]["total_tokens"] == 160
    assert "clave-ficticia-de-prueba" not in json.dumps(capturado["payload"])
    assert "respuesta es una recomendación" in capturado["payload"]["messages"][0]["content"]


def test_kilometro_cero_rechaza_accion_y_corredor_desconocidos(monkeypatch) -> None:
    for plan, motivo in (
        (plan_kilometro(recomendacion="despachar_todo"), "catálogo"),
        (plan_kilometro(corredores_sugeridos=["C-99"]), "no están en el escenario"),
        (plan_kilometro(acciones_alternas=["inventar_ruta"]), "no ofrece"),
        (plan_kilometro(evidencia=[]), "lista no vacía"),
    ):
        monkeypatch.setattr(cliente, "post_chat", lambda *a, _p=plan, **k: sobre(_p))
        with pytest.raises(cliente.LlmRespuestaInvalida, match=motivo):
            kilometro_cero.analizar(ajustes(), contexto_kilometro())


def test_kilometro_cero_sin_configuracion() -> None:
    with pytest.raises(cliente.LlmNoConfigurado):
        kilometro_cero.analizar(ajustes(**SIN_LLM), contexto_kilometro())


# ---------------------------------------------------------------- Ripple Room
def contexto_ripple() -> dict:
    return {
        "escenario": {"id": "asia", "nombre": "Corte de Asia", "descripcion": "Crisis"},
        "resultado": {"service": 91.2, "shortage": 3.4, "backlog": 12, "cost": 118_000},
        "roles": [
            {"id": "ops", "nombre": "Operaciones", "interes": "Continuidad"},
            {"id": "proc", "nombre": "Compras", "interes": "Costo"},
            {"id": "fin", "nombre": "Finanzas", "interes": "Margen"},
        ],
        "plan": {"id": "balanced", "nombre": "Equilibrado"},
    }


def plan_ripple(**extra) -> dict:
    datos = {
        "perspectivas": [
            {
                "rol_id": "ops",
                "postura": "apoyar",
                "justificacion": "El plan sostiene el servicio por encima del 90%.",
                "riesgo": "El atraso puede crecer si el corte se extiende.",
            },
            {
                "rol_id": "proc",
                "postura": "condicionar",
                "justificacion": "El costo sube 118 mil con el plan equilibrado.",
                "riesgo": "Depende de un proveedor alterno.",
            },
            {
                "rol_id": "fin",
                "postura": "objetar",
                "justificacion": "El margen cae con ese costo.",
                "riesgo": "Sin visibilidad del tipo de cambio.",
            },
        ]
    }
    datos.update(extra)
    return datos


def test_ripple_room_una_perspectiva_por_rol(monkeypatch) -> None:
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: sobre(plan_ripple()))
    salida = ripple_room.analizar(ajustes(), contexto_ripple())

    assert [fila["rol_id"] for fila in salida["perspectivas"]] == ["ops", "proc", "fin"]
    assert salida["una_llamada"] is True
    assert salida["agentes_independientes"] is False
    assert salida["marco"] == "simulacion"
    assert salida["motor_determinista_intacto"] is True
    assert {fila["postura"] for fila in salida["perspectivas"]} <= set(ripple_room.POSTURAS)


def test_ripple_room_exige_los_roles_exactos(monkeypatch) -> None:
    completo = plan_ripple()["perspectivas"]
    casos = [
        ({"perspectivas": completo[:2]}, "no cubre exactamente"),
        (
            {
                "perspectivas": [
                    completo[0],
                    completo[1],
                    {
                        "rol_id": "legal",
                        "postura": "apoyar",
                        "justificacion": "Sin objeción.",
                        "riesgo": "Ninguno.",
                    },
                ]
            },
            "no participa",
        ),
        ({"perspectivas": [completo[0], completo[0]]}, "repetido"),
        (
            {
                "perspectivas": [
                    {**completo[0], "postura": "vetar"},
                    completo[1],
                    completo[2],
                ]
            },
            "postura",
        ),
        ({"perspectivas": []}, "ninguna perspectiva"),
    ]
    for contenido, motivo in casos:
        monkeypatch.setattr(cliente, "post_chat", lambda *a, _c=contenido, **k: sobre(_c))
        with pytest.raises(cliente.LlmRespuestaInvalida, match=motivo):
            ripple_room.analizar(ajustes(), contexto_ripple())


# ------------------------------------------------------------------- DockMind
def contexto_dockmind() -> dict:
    return {
        "escenario": {"id": "promo", "nombre": "Promoción", "parametros": {"promo": 1.2}},
        "sku": {
            "sku": "8542",
            "nombre": "Producto 8542",
            "categoria": "bebidas",
            "lead": 4,
            "pack": 12,
            "stock": 480,
            "precio": 12.5,
            "costo": 8.1,
        },
        "pronostico": {"metodo": "seasonal", "wape": 0.18, "mae": 3.2, "horizonte": 12},
        "plan_compra": [{"semana": 1, "cantidad": 120, "costo": 972.0}],
        "restricciones": {"presupuesto": 250_000.0, "buffer_dias": 14},
        "metricas": {"valores": {"cobertura_semanas": 3.2, "riesgo_quiebre": 0.12, "exceso": 0.08}},
    }


def plan_dockmind(**extra) -> dict:
    datos = {
        "explicacion": "El plan cubre 3.2 semanas con un riesgo de quiebre del 12%.",
        "evidencia": ["WAPE 18% y cobertura de 3.2 semanas."],
        "riesgos": ["El buffer de 14 días es corto para el lead de 4 días."],
        "opciones": [
            {"opcion": "mantener_plan", "efecto_esperado": "Cobertura estable."},
            {"opcion": "ajustar_buffer", "efecto_esperado": "Reduce el riesgo de quiebre."},
        ],
    }
    datos.update(extra)
    return datos


def test_dockmind_explica_sin_recalcular(monkeypatch) -> None:
    capturado = {}

    def falso(settings, payload, *, timeout=None):
        capturado["payload"] = payload
        return sobre(plan_dockmind())

    monkeypatch.setattr(cliente, "post_chat", falso)
    salida = dockmind.analizar(ajustes(), contexto_dockmind())

    assert salida["modo"] == "openai"
    assert [fila["opcion"] for fila in salida["opciones"]] == ["mantener_plan", "ajustar_buffer"]
    assert salida["no_recalcula_pronostico"] is True
    assert salida["no_ordena_compras"] is True
    assert salida["no_modifica_inventario"] is True
    assert "clave-ficticia-de-prueba" not in json.dumps(capturado["payload"])


def test_dockmind_rechaza_opciones_fuera_del_catalogo(monkeypatch) -> None:
    for plan, motivo in (
        (plan_dockmind(opciones=[{"opcion": "comprar_ya", "efecto_esperado": "x"}]), "catálogo"),
        (
            plan_dockmind(
                opciones=[
                    {"opcion": "mantener_plan", "efecto_esperado": "a"},
                    {"opcion": "ajustar_buffer", "efecto_esperado": "b"},
                    {"opcion": "priorizar_sku", "efecto_esperado": "c"},
                    {"opcion": "revisar_pronostico", "efecto_esperado": "d"},
                ]
            ),
            "máximo 3",
        ),
        (plan_dockmind(explicacion="   "), "vacío"),
    ):
        monkeypatch.setattr(cliente, "post_chat", lambda *a, _p=plan, **k: sobre(_p))
        with pytest.raises(cliente.LlmRespuestaInvalida, match=motivo):
            dockmind.analizar(ajustes(), contexto_dockmind())


# ------------------------------------------------------------ rutas HTTP
def _app(rutas, settings: Settings) -> TestClient:
    app = FastAPI()
    for router in rutas:
        app.include_router(router.router, prefix="/api/v1")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[exigir_cuota_llm] = lambda: None
    return TestClient(app)


def test_rutas_responden_200_422_503_502(monkeypatch) -> None:
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: sobre(plan_kilometro()))
    cliente_http = _app([ruta_kilometro], ajustes())

    ok = cliente_http.post("/api/v1/kilometro-cero/analisis", json=contexto_kilometro())
    assert ok.status_code == 200
    assert ok.json()["recomendacion"]["accion"] == "desviar_por_corredor_alterno"

    # Entrada excesiva: 21 corredores superan el tope.
    excesivo = contexto_kilometro()
    excesivo["corredores"] = excesivo["corredores"] * 11
    assert cliente_http.post("/api/v1/kilometro-cero/analisis", json=excesivo).status_code == 422

    # Acción no permitida en la petición.
    invalido = contexto_kilometro()
    invalido["acciones_disponibles"] = ["despachar_todo"]
    assert cliente_http.post("/api/v1/kilometro-cero/analisis", json=invalido).status_code == 422

    # Sin configuración → 503; el detalle no revela credenciales.
    sin = _app([ruta_kilometro], ajustes(**SIN_LLM))
    respuesta = sin.post("/api/v1/kilometro-cero/analisis", json=contexto_kilometro())
    assert respuesta.status_code == 503
    assert "clave" not in respuesta.text.lower()

    # Respuesta inválida del modelo → 502.
    monkeypatch.setattr(
        cliente, "post_chat", lambda *a, **k: sobre(plan_kilometro(recomendacion="despachar_todo"))
    )
    assert cliente_http.post("/api/v1/kilometro-cero/analisis", json=contexto_kilometro()).status_code == 502


def test_rutas_de_ripple_y_dockmind(monkeypatch) -> None:
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: sobre(plan_ripple()))
    sitio = _app([ruta_ripple], ajustes())
    assert sitio.post("/api/v1/ripple-room/analisis", json=contexto_ripple()).status_code == 200
    roto = contexto_ripple()
    roto["roles"] = roto["roles"] * 3  # 9 roles superan el tope de 6
    assert sitio.post("/api/v1/ripple-room/analisis", json=roto).status_code == 422

    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: sobre(plan_dockmind()))
    otro = _app([ruta_dockmind], ajustes())
    assert otro.post("/api/v1/dockmind/analisis", json=contexto_dockmind()).status_code == 200
    excesivo = contexto_dockmind()
    excesivo["plan_compra"] = [{"semana": i, "cantidad": 1, "costo": 1.0} for i in range(27)]
    assert otro.post("/api/v1/dockmind/analisis", json=excesivo).status_code == 422


def test_limite_de_frecuencia_responde_429(monkeypatch) -> None:
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: sobre(plan_dockmind()))
    sitio = _app([ruta_dockmind], ajustes(llm_consultas_por_minuto=2))
    assert sitio.post("/api/v1/dockmind/analisis", json=contexto_dockmind()).status_code == 200
    assert sitio.post("/api/v1/dockmind/analisis", json=contexto_dockmind()).status_code == 200
    tercera = sitio.post("/api/v1/dockmind/analisis", json=contexto_dockmind())
    assert tercera.status_code == 429
    assert "Límite" in tercera.json()["detail"]
