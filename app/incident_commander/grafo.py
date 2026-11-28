"""Grafo LangGraph del agente supervisor de Data Incident Commander.

El grafo representa explícitamente el estado de la investigación: incidente,
evidencia consultada, presupuesto restante, errores y recomendación. El modelo
elige una herramienta de solo lectura, el servidor valida nombre y argumentos,
la herramienta devuelve un resumen y el modelo decide el siguiente paso. La
recomendación final vuelve a validarse contra el catálogo de planes y contra la
evidencia efectivamente consultada.

Condiciones de parada: máximo de pasos, tiempo de pared, presupuesto de tokens y
evidencia insuficiente. La revisión humana no se sustituye: aprobar y simular la
reparación siguen siendo responsabilidad del flujo determinista y de `aprobar`.
"""

from __future__ import annotations

import json
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.config import Settings
from app.incident_commander import herramientas as catalogo
from app.incident_commander import observabilidad
from app.incident_commander import service as dic
from app.llm import cliente
from app.llm import contexto as contexto_llm
from app.llm import limites as limites_llm

SISTEMA = (
    "Eres un agente supervisor de diagnóstico de calidad de datos. Tienes "
    "herramientas de SOLO LECTURA; úsalas para reunir evidencia antes de "
    "responder. Elige la herramienta siguiente según lo que acabas de observar: "
    "perfil_copia, linaje, validaciones, estado_run, matriz_planes, "
    "catalogo_planes o snapshot. No puedes aprobar incidentes, cambiar estados, "
    "escribir datos ni ejecutar remediaciones. Cuando tengas evidencia suficiente, "
    "responde SOLO con JSON: "
    '{"recommendation":"quarantine|backfill|fullreload","justification":"...",'
    '"evidence":["herramienta: observación concreta"],"risks":["..."]}. '
    "Cada elemento de evidence debe empezar por el nombre exacto de una "
    "herramienta que hayas consultado, seguido de dos puntos y un dato observado. "
    "Solo puedes recomendar quarantine, backfill o fullreload; «forward» está "
    "bloqueado. No inventes evidencia ni consultas. Tus recomendaciones son "
    "consultivas. Responde en español."
)

CHARS_POR_TOKEN = 3.6


class AgenteLimitePasos(RuntimeError):
    """Se alcanzó el máximo de pasos sin respuesta final."""

    def __init__(
        self,
        mensaje: str,
        *,
        traza: list[dict[str, Any]] | None = None,
        eventos: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(mensaje)
        self.traza = list(traza or [])
        self.eventos = list(eventos or [])


class AgentePresupuesto(RuntimeError):
    """Se agotó el presupuesto de tokens del agente."""

    def __init__(
        self,
        mensaje: str,
        *,
        traza: list[dict[str, Any]] | None = None,
        eventos: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(mensaje)
        self.traza = list(traza or [])
        self.eventos = list(eventos or [])


class AgentePolitica(RuntimeError):
    """El modelo intentó una herramienta, argumento o acción fuera de política."""

    def __init__(
        self,
        mensaje: str,
        *,
        traza: list[dict[str, Any]] | None = None,
        eventos: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(mensaje)
        self.traza = list(traza or [])
        self.eventos = list(eventos or [])


class AgenteTiempo(RuntimeError):
    """La corrida superó el tiempo de pared permitido."""

    def __init__(
        self,
        mensaje: str,
        *,
        traza: list[dict[str, Any]] | None = None,
        eventos: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(mensaje)
        self.traza = list(traza or [])
        self.eventos = list(eventos or [])


class EstadoAgente(TypedDict, total=False):
    run_id: str
    scenario_id: str
    mensajes: list[dict[str, Any]]
    traza: list[dict[str, Any]]
    eventos: list[dict[str, Any]]
    evidencia: list[dict[str, Any]]
    herramientas_usadas: list[str]
    llamadas: list[dict[str, Any]]
    recomendacion: dict[str, Any] | None
    pasos: int
    llamadas_modelo: int
    llamadas_herramientas: int
    tokens_consumidos: int
    presupuesto_tokens: int
    max_pasos: int
    max_segundos: float
    inicio_monotonic: float
    intentos_salida: int
    finalizado: bool
    estado: str
    motivo_parada: str | None


def _evento(
    estado: EstadoAgente,
    tipo: str,
    *,
    actor: str,
    resumen: str,
    herramienta: str | None = None,
    evidencia_id: str | None = None,
    **extras: Any,
) -> dict[str, Any]:
    """Evento observable y acotado; nunca incluye prompts ni razonamiento interno."""
    duracion = 0
    if estado.get("inicio_monotonic"):
        duracion = int((time.monotonic() - float(estado["inicio_monotonic"])) * 1000)
    evento = {
        "seq": len(estado["eventos"]) + 1,
        "tipo": tipo,
        "actor": actor,
        "resumen": resumen[:240],
        "duracion_ms": max(0, duracion),
        "paso": int(estado.get("pasos", 0)),
    }
    if herramienta:
        evento["herramienta"] = herramienta
    if evidencia_id:
        evento["evidencia_id"] = evidencia_id
    for clave, valor in extras.items():
        if isinstance(valor, (str, int, float, bool)) or valor is None:
            evento[clave] = valor[:240] if isinstance(valor, str) else valor
    estado["eventos"].append(evento)
    return evento


def _traza_paso(paso: int, tipo: str, **datos: Any) -> dict[str, Any]:
    entrada = {"paso": paso, "tipo": tipo}
    for clave, valor in datos.items():
        if isinstance(valor, str):
            entrada[clave] = valor[:160]
        elif isinstance(valor, (int, float, bool)) or valor is None:
            entrada[clave] = valor
        else:
            entrada[clave] = str(valor)[:160]
    return entrada


def _estimar_tokens(
    mensajes: list[dict[str, Any]], salida_tope: int, fijo_chars: int = 0
) -> int:
    caracteres = int(fijo_chars)
    for mensaje in mensajes:
        contenido = mensaje.get("content")
        if isinstance(contenido, str):
            caracteres += len(contenido)
        llamadas = mensaje.get("tool_calls")
        if llamadas:
            caracteres += len(json.dumps(llamadas, ensure_ascii=False))
    return int(round(caracteres / CHARS_POR_TOKEN)) + int(salida_tope)


def _validar_evidencia(evidencia: list[str], usadas: list[str]) -> tuple[bool, str]:
    """La evidencia debe citar una herramienta realmente ejecutada."""
    if not usadas:
        return False, "No se consultó ninguna herramienta de solo lectura."
    permitidas = set(usadas)
    for item in evidencia:
        prefijo = item.split(":", 1)[0].strip()
        if prefijo not in permitidas:
            return False, (
                "Cada evidencia debe empezar por una herramienta consultada "
                "(por ejemplo «validaciones: …»)."
            )
    return True, ""


def _comprobar_limites(estado: EstadoAgente) -> None:
    if int(estado["pasos"]) >= int(estado["max_pasos"]):
        _evento(
            estado,
            "parada",
            actor="servidor",
            resumen=f"Se alcanzó el máximo de {estado['max_pasos']} pasos; no se ejecutó ninguna reparación.",
            motivo="limite_pasos",
        )
        raise AgenteLimitePasos(
            f"El agente no cerró la consulta en {estado['max_pasos']} pasos; no se devuelve recomendación.",
            traza=estado.get("traza"),
            eventos=estado.get("eventos"),
        )
    transcurrido = time.monotonic() - float(estado["inicio_monotonic"])
    if transcurrido > float(estado["max_segundos"]):
        _evento(
            estado,
            "parada",
            actor="servidor",
            resumen="Se superó el tiempo máximo de investigación; no se ejecutó ninguna reparación.",
            motivo="tiempo",
        )
        raise AgenteTiempo(
            "El agente superó el tiempo máximo de investigación; se detuvo sin recomendación.",
            traza=estado.get("traza"),
            eventos=estado.get("eventos"),
        )


def _compilar(servicio: dic.IncidentCommander, settings: Settings):
    esquemas = catalogo.esquemas()
    esquemas_chars = len(json.dumps(esquemas, ensure_ascii=False))

    def preparar(estado: EstadoAgente) -> EstadoAgente:
        run = servicio._run(estado["run_id"])
        contexto = servicio._contexto_consulta(run)
        estado["mensajes"] = [
            {"role": "system", "content": SISTEMA},
            {"role": "user", "content": cliente.recortar(contexto)},
        ]
        _evento(
            estado,
            "entrada",
            actor="sistema",
            resumen=f"Incidente {estado['scenario_id']} recibido; autoridad de escritura: ninguna.",
        )
        return estado

    def modelo(estado: EstadoAgente) -> EstadoAgente:
        _comprobar_limites(estado)
        salida_tope = min(int(settings.llm_max_tokens), 300)
        estimado = _estimar_tokens(estado["mensajes"], salida_tope, esquemas_chars)
        if int(estado["tokens_consumidos"]) + estimado > int(estado["presupuesto_tokens"]):
            estado["traza"].append(
                _traza_paso(int(estado["pasos"]) + 1, "presupuesto", motivo="estimacion")
            )
            _evento(
                estado,
                "parada",
                actor="servidor",
                resumen="El siguiente paso no cabe en el presupuesto de tokens; no se ejecutó ninguna reparación.",
                motivo="presupuesto",
            )
            raise AgentePresupuesto(
                "El agente alcanzó su presupuesto de tokens; se detuvo sin otra llamada.",
                traza=estado.get("traza"),
                eventos=estado.get("eventos"),
            )

        estado["pasos"] = int(estado.get("pasos", 0)) + 1
        estado["llamadas_modelo"] = int(estado.get("llamadas_modelo", 0)) + 1
        with observabilidad.traza_modelo(
            paso=int(estado["pasos"]), modelo=str(settings.llm_model)
        ) as span:
            sobre = cliente.solicitar_chat(
                settings,
                estado["mensajes"],
                herramientas=esquemas,
                max_tokens=salida_tope,
            )
            uso = cliente.uso(sobre)
            respuesta = cliente.mensaje(sobre)
            llamadas = respuesta.get("tool_calls") or []
            nombres = [
                str((llamada.get("function") or {}).get("name") or "")
                for llamada in llamadas
                if isinstance(llamada, dict)
            ]
            observabilidad.anotar_modelo(
                span,
                uso=uso,
                herramientas=nombres,
                final=not bool(llamadas),
            )
        if uso and isinstance(uso.get("total_tokens"), int):
            estado["tokens_consumidos"] = int(estado.get("tokens_consumidos", 0)) + int(
                uso["total_tokens"]
            )
        if int(estado["tokens_consumidos"]) > int(estado["presupuesto_tokens"]):
            estado["traza"].append(
                _traza_paso(int(estado["pasos"]), "presupuesto", motivo="consumo")
            )
            _evento(
                estado,
                "parada",
                actor="servidor",
                resumen="Se agotó el presupuesto de tokens sin cerrar la consulta; no se ejecutó ninguna reparación.",
                motivo="presupuesto",
            )
            raise AgentePresupuesto(
                "El agente agotó su presupuesto de tokens sin cerrar la consulta.",
                traza=estado.get("traza"),
                eventos=estado.get("eventos"),
            )

        if llamadas:
            estado["llamadas"] = [
                llamada for llamada in llamadas if isinstance(llamada, dict)
            ]
            nombres = [
                str((llamada.get("function") or {}).get("name") or "")
                for llamada in estado["llamadas"]
            ]
            _evento(
                estado,
                "modelo",
                actor="modelo",
                resumen="El modelo recibió la evidencia acumulada y eligió continuar.",
                tokens=int(uso.get("total_tokens", 0)) if uso else None,
            )
            _evento(
                estado,
                "seleccion",
                actor="modelo",
                resumen="Herramienta elegida: " + ", ".join(nombres) + ".",
                herramienta=", ".join(nombres)[:120],
            )
            return estado

        contenido = str(respuesta.get("content") or "").strip()
        if not contenido:
            raise cliente.LlmUpstreamError("El proveedor devolvió una respuesta vacía.")
        try:
            plan = dic.validar_recomendacion(contenido)
        except cliente.LlmRespuestaInvalida:
            estado["intentos_salida"] = int(estado.get("intentos_salida", 0)) + 1
            _evento(
                estado,
                "salida_invalida",
                actor="servidor",
                resumen="La salida no cumplió el esquema; se pidió corregirla.",
            )
            if int(estado["intentos_salida"]) >= 2:
                raise
            estado["mensajes"].append(
                {
                    "role": "user",
                    "content": "La respuesta no cumplió el esquema. Devuelve solo el JSON pactado.",
                }
            )
            return estado

        if not estado.get("herramientas_usadas"):
            estado["estado"] = "detenido"
            estado["motivo_parada"] = "evidencia_insuficiente"
            _evento(
                estado,
                "parada",
                actor="servidor",
                resumen="El modelo no consultó herramientas; no hay evidencia verificable.",
                motivo="evidencia_insuficiente",
            )
            return estado

        valida, motivo = _validar_evidencia(plan["evidence"], estado.get("herramientas_usadas", []))
        if not valida:
            estado["intentos_salida"] = int(estado.get("intentos_salida", 0)) + 1
            _evento(
                estado,
                "validacion",
                actor="servidor",
                resumen="La evidencia no cita una herramienta consultada; se rechazó.",
                causa="evidencia_no_vinculada",
            )
            if int(estado["intentos_salida"]) >= 2:
                raise cliente.LlmRespuestaInvalida(motivo)
            estado["mensajes"].append({"role": "user", "content": motivo})
            return estado

        estado["recomendacion"] = plan
        estado["finalizado"] = True
        estado["estado"] = "completado"
        estado["traza"].append(_traza_paso(int(estado["pasos"]), "final", plan=plan["plan"]))
        _evento(
            estado,
            "modelo",
            actor="modelo",
            resumen="El modelo cerró la investigación con una recomendación.",
        )
        _evento(
            estado,
            "validacion",
            actor="servidor",
            resumen="Esquema, política y vínculo de evidencia validados en el servidor.",
            estado_validacion="aprobada",
        )
        _evento(
            estado,
            "recomendacion",
            actor="modelo",
            resumen=f"Recomendación consultiva: {plan['plan']}.",
            plan=plan["plan"],
        )
        return estado

    def herramienta(estado: EstadoAgente) -> EstadoAgente:
        for llamada in estado.get("llamadas") or []:
            funcion = llamada.get("function") or {}
            nombre = str(funcion.get("name") or "")
            argumentos = funcion.get("arguments")
            try:
                definicion, validos = catalogo.validar(nombre, argumentos)
            except catalogo.HerramientaDesconocida as exc:
                estado["traza"].append(
                    _traza_paso(int(estado["pasos"]), "rechazo", herramienta=nombre, motivo="fuera_de_catalogo")
                )
                _evento(
                    estado,
                    "rechazo",
                    actor="servidor",
                    resumen=f"Herramienta fuera del catálogo: {nombre}.",
                    herramienta=nombre,
                    motivo="fuera_de_catalogo",
                )
                raise AgentePolitica(
                    f"El modelo pidió una herramienta no permitida: {nombre}",
                    traza=estado.get("traza"),
                    eventos=estado.get("eventos"),
                ) from exc
            except catalogo.ArgumentosInvalidos as exc:
                estado["traza"].append(
                    _traza_paso(int(estado["pasos"]), "rechazo", herramienta=nombre, motivo="argumentos_invalidos")
                )
                _evento(
                    estado,
                    "rechazo",
                    actor="servidor",
                    resumen=f"Argumentos inválidos para {nombre}; no se ejecutó.",
                    herramienta=nombre,
                    motivo="argumentos_invalidos",
                )
                raise AgentePolitica(
                    f"Argumentos inválidos para {nombre}.",
                    traza=estado.get("traza"),
                    eventos=estado.get("eventos"),
                ) from exc

            try:
                with observabilidad.traza_herramienta(
                    nombre=nombre, paso=int(estado["pasos"])
                ) as span:
                    resultado = definicion.ejecutar(servicio, estado["run_id"], validos)
                    observabilidad.anotar_herramienta(span, resultado)
            except dic.ScenarioError as exc:
                _evento(
                    estado,
                    "rechazo",
                    actor="servidor",
                    resumen=f"La herramienta {nombre} rechazó el contexto.",
                    herramienta=nombre,
                    motivo="contexto_invalido",
                )
                raise AgentePolitica(
                    str(exc), traza=estado.get("traza"), eventos=estado.get("eventos")
                ) from exc

            estado["llamadas_herramientas"] = int(estado.get("llamadas_herramientas", 0)) + 1
            usadas = estado.setdefault("herramientas_usadas", [])
            if nombre not in usadas:
                usadas.append(nombre)
            estado.setdefault("evidencia", []).append(
                {
                    "herramienta": nombre,
                    "evidencia_id": resultado.get("evidencia_id", nombre),
                    "resumen": resultado.get("resumen", ""),
                }
            )
            estado["mensajes"].append(
                {"role": "assistant", "content": "", "tool_calls": [llamada]}
            )
            estado["mensajes"].append(
                {
                    "role": "tool",
                    "tool_call_id": llamada.get("id", f"call-{estado['pasos']}"),
                    "content": json.dumps(resultado, ensure_ascii=False)[:2000],
                }
            )
            estado["traza"].append(
                _traza_paso(
                    int(estado["pasos"]),
                    "herramienta",
                    herramienta=nombre,
                    evidencia_id=resultado.get("evidencia_id"),
                )
            )
            _evento(
                estado,
                "herramienta",
                actor="herramienta",
                resumen=resultado.get("resumen", "Herramienta ejecutada."),
                herramienta=nombre,
                evidencia_id=resultado.get("evidencia_id"),
                solo_lectura=True,
            )
            _evento(
                estado,
                "resultado",
                actor="herramienta",
                resumen="Resultado devuelto al modelo y agregado a la evidencia.",
                herramienta=nombre,
                evidencia_id=resultado.get("evidencia_id"),
            )
        estado["llamadas"] = []
        return estado

    def final(estado: EstadoAgente) -> EstadoAgente:
        _evento(
            estado,
            "revision_humana",
            actor="humano",
            resumen="Recomendación pendiente de revisión humana; no se ejecutó nada.",
            estado_revision="pendiente",
        )
        return estado

    def parada(estado: EstadoAgente) -> EstadoAgente:
        if not estado.get("eventos") or estado["eventos"][-1].get("tipo") != "parada":
            _evento(
                estado,
                "parada",
                actor="servidor",
                resumen="La corrida se detuvo sin recomendación.",
                motivo=estado.get("motivo_parada") or "sin_recomendacion",
            )
        return estado

    def decidir(estado: EstadoAgente) -> str:
        if estado.get("finalizado"):
            return "final"
        if estado.get("estado") == "detenido":
            return "parada"
        if estado.get("llamadas"):
            return "herramienta"
        return "reintentar"

    constructor = StateGraph(EstadoAgente)
    constructor.add_node("preparar", preparar)
    constructor.add_node("modelo", modelo)
    constructor.add_node("herramienta", herramienta)
    constructor.add_node("final", final)
    constructor.add_node("parada", parada)
    constructor.add_edge(START, "preparar")
    constructor.add_edge("preparar", "modelo")
    constructor.add_conditional_edges(
        "modelo",
        decidir,
        {
            "herramienta": "herramienta",
            "final": "final",
            "parada": "parada",
            "reintentar": "modelo",
        },
    )
    constructor.add_edge("herramienta", "modelo")
    constructor.add_edge("final", END)
    constructor.add_edge("parada", END)
    return constructor.compile()


def _resultado(estado: EstadoAgente, *, modelo: str) -> dict[str, Any]:
    plan = estado.get("recomendacion")
    base: dict[str, Any] = {
        "run_id": estado["run_id"],
        "modo": "langgraph-openai-tools",
        "flujo": "langgraph",
        "modelo": modelo,
        "traza": estado.get("traza", []),
        "eventos": estado.get("eventos", []),
        "pasos": int(estado.get("pasos", 0)),
        "llamadas_modelo": int(estado.get("llamadas_modelo", 0)),
        "llamadas_herramientas": int(estado.get("llamadas_herramientas", 0)),
        "tokens_consumidos": int(estado.get("tokens_consumidos", 0)),
        "presupuesto_tokens": int(estado.get("presupuesto_tokens", 0)),
        "herramientas_usadas": list(estado.get("herramientas_usadas", [])),
        "herramientas_disponibles": sorted(catalogo.HERRAMIENTAS),
        "solo_lectura": True,
        "advisory_only": True,
        "approved": False,
        "executed": False,
        "external_writes": 0,
        "human_approval": "pending",
        "observabilidad": observabilidad.estado_publico(),
    }
    if not plan:
        base.update(
            {
                "estado": "detenido",
                "motivo_parada": estado.get("motivo_parada") or "sin_recomendacion",
                "recomendacion": None,
                "justificacion": "",
                "evidencia": [],
                "riesgos": [],
                "evidencia_insuficiente": estado.get("motivo_parada")
                == "evidencia_insuficiente",
            }
        )
        return base
    base.update(
        {
            "estado": "completado",
            "motivo_parada": None,
            "evidencia_insuficiente": False,
            "recomendacion": {
                "plan": plan["plan"],
                "name": plan["plan_name"],
                "allowed": True,
                "forward_blocked": True,
            },
            "justificacion": plan["justification"],
            "evidencia": plan["evidence"],
            "riesgos": plan["risks"],
            "evidencia_consultada": list(estado.get("evidencia", [])),
        }
    )
    return base


def ejecutar(
    servicio: dic.IncidentCommander,
    run_id: str,
    settings: Settings,
    *,
    max_pasos: int | None = None,
    presupuesto_tokens: int | None = None,
    max_segundos: float | None = None,
) -> dict[str, Any]:
    """Ejecuta el grafo; reserva una sola vez el cupo y el presupuesto del run."""
    run = servicio._run(run_id)  # RunNotFound si no existe
    if not cliente.configurado(settings):
        raise cliente.LlmNoConfigurado(
            "La consulta a OpenAI no está configurada en este despliegue."
        )

    pasos_maximos = int(max_pasos or settings.llm_agente_max_pasos)
    presupuesto = int(presupuesto_tokens or settings.llm_agente_presupuesto_tokens)
    segundos_maximos = float(max_segundos or settings.llm_agente_max_segundos)
    limites_llm.reservar_run(
        settings, ip=contexto_llm.ip_actual(), presupuesto_tokens=presupuesto
    )

    estado: EstadoAgente = {
        "run_id": run_id,
        "scenario_id": str(run["scenario_id"]),
        "mensajes": [],
        "traza": [],
        "eventos": [],
        "evidencia": [],
        "herramientas_usadas": [],
        "llamadas": [],
        "recomendacion": None,
        "pasos": 0,
        "llamadas_modelo": 0,
        "llamadas_herramientas": 0,
        "tokens_consumidos": 0,
        "presupuesto_tokens": presupuesto,
        "max_pasos": pasos_maximos,
        "max_segundos": segundos_maximos,
        "inicio_monotonic": time.monotonic(),
        "intentos_salida": 0,
        "finalizado": False,
        "estado": "investigando",
        "motivo_parada": None,
    }

    with observabilidad.traza_run(
        run_id=run_id,
        scenario_id=str(run["scenario_id"]),
        modo=observabilidad.modo_para_modelo(str(settings.llm_model)),
    ) as span_run:
        with contexto_llm.reserva_de_run():
            grafo = _compilar(servicio, settings)
            resultado_estado = grafo.invoke(
                estado,
                config={"recursion_limit": max(25, pasos_maximos * 3 + 5)},
            )
        resultado = _resultado(resultado_estado, modelo=str(settings.llm_model))
        observabilidad.anotar_run(span_run, resultado)
        return resultado
