"""Kilómetro Cero · lectura consultiva del escenario logístico.

La demo es un simulador: `DATA` con corredores, envíos e incidencias, y un
`incident` activo que solo repinta la red. Esta ruta recibe un resumen acotado de
ese estado y devuelve una recomendación **consultiva**: no mueve rutas, no
despacha envíos y no escribe nada. Los datos son sintéticos por diseño.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.llm import cliente

#: Acciones que la demo sabe representar; el modelo no puede inventar otras.
ACCIONES = (
    "desviar_por_corredor_alterno",
    "reprogramar_entrega",
    "priorizar_carga_critica",
    "reforzar_capacidad",
    "pausar_despacho",
)

SISTEMA = (
    "Eres un analista de operaciones logísticas. Recibes el estado de un "
    "simulador sintético de una red de carreteras en México: escenario activo, "
    "corredores con envíos y atrasos, e incidencias. Explica los riesgos y "
    "recomienda acciones del simulador. Devuelve SOLO JSON con el esquema "
    '{"recomendacion":"<accion permitida>","justificacion":"...",'
    '"evidencia":["..."],"riesgos":["..."],"corredores_sugeridos":["<id>"],'
    '"acciones_alternas":["<accion permitida>"]}. '
    "Reglas: usa únicamente ids de corredor y acciones de las listas recibidas; "
    "no inventes datos ni cifras nuevas; no afirmes que los datos son reales "
    "(son sintéticos); no digas que despachas, desvías ni aplicas nada: tu "
    "respuesta es una recomendación que una persona aplicará en el simulador. "
    "Una o dos frases de justificación, entre una y cuatro evidencias y entre "
    "una y cuatro riesgos, todos breves."
)


def _validar_corredores(valor: Any, permitidos: set[str]) -> list[str]:
    if valor is None:
        return []
    if not isinstance(valor, list):
        raise cliente.LlmRespuestaInvalida("«corredores_sugeridos» debe ser una lista.")
    if len(valor) > 6:
        raise cliente.LlmRespuestaInvalida(
            "«corredores_sugeridos» admite como máximo 6 elementos."
        )
    limpios = [cliente.texto(item, campo="corredores_sugeridos", maximo=40) for item in valor]
    if set(limpios) - permitidos:
        raise cliente.LlmRespuestaInvalida(
            "La recomendación cita corredores que no están en el escenario."
        )
    return limpios


def _validar_acciones(valor: Any) -> list[str]:
    if valor is None:
        return []
    if not isinstance(valor, list) or len(valor) > 4:
        raise cliente.LlmRespuestaInvalida("«acciones_alternas» debe ser una lista corta.")
    limpios = [cliente.texto(item, campo="acciones_alternas", maximo=40) for item in valor]
    if set(limpios) - set(ACCIONES):
        raise cliente.LlmRespuestaInvalida(
            "La recomendación incluye una acción que el simulador no ofrece."
        )
    return limpios


def analizar(settings: Settings, contexto: dict[str, Any]) -> dict[str, Any]:
    """Devuelve la recomendación validada del modelo para el escenario activo."""
    permitidos = {fila["id"] for fila in contexto["corredores"]}

    def validar(datos: dict[str, Any]) -> dict[str, Any]:
        accion = cliente.texto(
            datos.get("recomendacion"), campo="recomendacion", maximo=40
        )
        if accion not in ACCIONES:
            raise cliente.LlmRespuestaInvalida(
                "La acción recomendada no está en el catálogo del simulador."
            )
        return {
            "accion": accion,
            "justificacion": cliente.texto(
                datos.get("justificacion"), campo="justificacion", maximo=600
            ),
            "evidencia": cliente.lista(
                datos.get("evidencia"), campo="evidencia", maximo=4, largo=220
            ),
            "riesgos": cliente.lista(
                datos.get("riesgos"), campo="riesgos", maximo=4, largo=220
            ),
            "corredores_sugeridos": _validar_corredores(
                datos.get("corredores_sugeridos"), permitidos
            ),
            "acciones_alternas": _validar_acciones(datos.get("acciones_alternas")),
        }

    limpio, sobre = cliente.solicitar_json(
        settings,
        clave_limite="kilometro-cero",
        sistema=SISTEMA,
        datos=contexto,
        validar=validar,
        max_tokens=min(int(settings.llm_max_tokens), 600),
    )
    return {
        "modo": "openai",
        "modelo": str(settings.llm_model),
        "recomendacion": limpio,
        "aviso": "simulacion",
        "datos_reales": False,
        "advisory_only": True,
        "puede_despachar": False,
        "puede_modificar_rutas": False,
        "acciones_permitidas": list(ACCIONES),
        "uso": cliente.uso(sobre),
        "cargos_posibles": True,
    }
