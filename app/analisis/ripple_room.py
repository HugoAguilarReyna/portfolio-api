"""The Ripple Room · perspectivas consultivas de los roles del simulador.

La demo simula una crisis de suministro y muestra tres «agentes» (operaciones,
compras, finanzas) con mensajes generados por reglas locales. Esta ruta pide al
modelo **una sola llamada** que devuelva una perspectiva por rol, con postura,
justificación y riesgo, a partir del escenario y del resultado determinista. No
son agentes independientes y no sustituyen al motor: la simulación sigue siendo
local y determinista.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.llm import cliente

#: Posturas que la interfaz sabe pintar.
POSTURAS = ("apoyar", "condicionar", "objetar")

SISTEMA = (
    "Eres un analista de riesgo de cadena de suministro. Recibes un escenario "
    "sintético de una crisis de exportación y el resultado que ya calculó un "
    "motor determinista (nivel de servicio, faltantes, atraso y costo). Debes "
    "devolver, en UNA sola respuesta, la perspectiva de cada rol indicado. "
    'Devuelve SOLO JSON con el esquema {"perspectivas":[{"rol_id":"<id>",'
    '"postura":"apoyar|condicionar|objetar","justificacion":"...",'
    '"riesgo":"..."}]}. '
    "Reglas: incluye exactamente los rol_id recibidos, sin añadir ni omitir "
    "ninguno; cada justificación y cada riesgo son una frase breve; usa las "
    "cifras del resultado como evidencia y no inventes otras; no afirmes que los "
    "datos son reales (son simulados); no ejecutes el plan ni digas que lo "
    "apruebas: es una perspectiva consultiva."
)


def analizar(settings: Settings, contexto: dict[str, Any]) -> dict[str, Any]:
    """Una llamada al modelo; devuelve una perspectiva validada por rol."""
    roles = {rol["id"]: rol["nombre"] for rol in contexto["roles"]}

    def validar(datos: dict[str, Any]) -> dict[str, Any]:
        perspectivas = datos.get("perspectivas")
        if not isinstance(perspectivas, list) or not perspectivas:
            raise cliente.LlmRespuestaInvalida(
                "El modelo no devolvió ninguna perspectiva."
            )
        if len(perspectivas) > len(roles):
            raise cliente.LlmRespuestaInvalida(
                "El modelo devolvió más perspectivas que roles hay en la escena."
            )
        vistas: dict[str, dict[str, str]] = {}
        for fila in perspectivas:
            if not isinstance(fila, dict):
                raise cliente.LlmRespuestaInvalida("Cada perspectiva debe ser un objeto.")
            rol_id = cliente.texto(fila.get("rol_id"), campo="rol_id", maximo=40)
            if rol_id not in roles:
                raise cliente.LlmRespuestaInvalida(
                    "La perspectiva cita un rol que no participa en la escena."
                )
            if rol_id in vistas:
                raise cliente.LlmRespuestaInvalida("Hay un rol repetido en la respuesta.")
            postura = cliente.texto(fila.get("postura"), campo="postura", maximo=20)
            if postura not in POSTURAS:
                raise cliente.LlmRespuestaInvalida(
                    "La postura debe ser apoyar, condicionar u objetar."
                )
            vistas[rol_id] = {
                "rol_id": rol_id,
                "rol": roles[rol_id],
                "postura": postura,
                "justificacion": cliente.texto(
                    fila.get("justificacion"), campo="justificacion", maximo=400
                ),
                "riesgo": cliente.texto(fila.get("riesgo"), campo="riesgo", maximo=300),
            }
        if set(vistas) != set(roles):
            raise cliente.LlmRespuestaInvalida(
                "La respuesta no cubre exactamente los roles de la escena."
            )
        return {"perspectivas": [vistas[rol_id] for rol_id in roles]}

    limpio, sobre = cliente.solicitar_json(
        settings,
        clave_limite="ripple-room",
        sistema=SISTEMA,
        datos=contexto,
        validar=validar,
        max_tokens=min(int(settings.llm_max_tokens), 700),
    )
    return {
        "modo": "openai",
        "modelo": str(settings.llm_model),
        "perspectivas": limpio["perspectivas"],
        "una_llamada": True,
        "agentes_independientes": False,
        "marco": "simulacion",
        "datos_reales": False,
        "motor_determinista_intacto": True,
        "advisory_only": True,
        "posturas_permitidas": list(POSTURAS),
        "uso": cliente.uso(sobre),
        "cargos_posibles": True,
    }
