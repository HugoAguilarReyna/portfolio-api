"""Cliente único del proveedor compatible con OpenAI para las demos.

Todas las rutas que consultan al modelo pasan por aquí: una sola forma de
construir la petición (`/chat/completions`), un solo lugar donde se aplican el
timeout, el tope de tokens, el recorte del contexto y el control de frecuencia.
La credencial viaja únicamente en la cabecera `Authorization` del proceso de la
API: no se registra, no se devuelve y nunca se incluye en un cuerpo.

Si falta cualquiera de las tres variables (`LLM_BASE_URL`, `LLM_API_KEY`,
`LLM_MODEL`), las rutas responden 503 y cada demo conserva su camino
determinista.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import Settings
from app.llm import contexto, limites

#: Tope duro de caracteres que se envían como contexto al modelo.
MAXIMO_CONTEXTO = 6_000
#: Tope duro de tokens de salida por consulta.
MAXIMO_TOKENS = 700


#: Errores de cuota, reexportados para que las rutas los distingan.
CuotaExcedida = limites.CuotaExcedida
LimitesNoConfigurados = limites.LimitesNoConfigurados
LimitesNoDisponibles = limites.LimitesNoDisponibles


class LlmNoConfigurado(RuntimeError):
    """Falta alguna de las tres variables del proveedor."""


class LlmUpstreamError(RuntimeError):
    """El proveedor no respondió, tardó demasiado o devolvió un sobre ilegible."""


class LlmRespuestaInvalida(ValueError):
    """El contenido no cumple el esquema pactado con la demo."""


class LlmLimiteExcedido(RuntimeError):
    """Se superó el número de consultas permitidas en la ventana de tiempo."""


def configurado(settings: Settings) -> bool:
    """Solo indica presencia; nunca expone ni compara valores."""
    return bool(
        (settings.llm_base_url or "").strip()
        and (settings.llm_api_key or "").strip()
        and (settings.llm_model or "").strip()
    )


def modelo(settings: Settings) -> str | None:
    return (str(settings.llm_model).strip() or None) if configurado(settings) else None


# --------------------------------------------------------------- control de uso
class _Ventana:
    """Ventana deslizante mínima por proceso, suficiente para proteger el gasto."""

    def __init__(self) -> None:
        self._marcas: dict[str, list[float]] = {}
        self._candado = threading.Lock()

    def consumir(self, clave: str, *, maximo: int, ventana_s: float) -> None:
        ahora = time.monotonic()
        with self._candado:
            marcas = [t for t in self._marcas.get(clave, []) if ahora - t < ventana_s]
            if len(marcas) >= maximo:
                espera = max(1, int(ventana_s - (ahora - marcas[0])) + 1)
                raise LlmLimiteExcedido(
                    f"Límite de consultas alcanzado; vuelve a intentarlo en {espera} s."
                )
            marcas.append(ahora)
            self._marcas[clave] = marcas

    def reiniciar(self) -> None:
        with self._candado:
            self._marcas.clear()


VENTANA = _Ventana()


def consumir(settings: Settings, clave: str) -> None:
    """Aplica el límite configurado (`llm_consultas_por_minuto`)."""
    VENTANA.consumir(
        clave,
        maximo=int(settings.llm_consultas_por_minuto),
        ventana_s=60.0,
    )


# ------------------------------------------------------------------ transporte
def _endpoint(settings: Settings) -> str:
    return f"{str(settings.llm_base_url).rstrip('/')}/chat/completions"


def post_chat(
    settings: Settings,
    payload: dict[str, Any],
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """POST a `/chat/completions` con reserva de cuota previa.

    Es el único transporte de las seis demos, así que aquí se aplican las cuotas
    compartidas (IP anónima + cupo global) y el tope de salida. Si la reserva no
    se puede hacer, no se llama al proveedor: falla cerrado.
    """
    # Reserva atómica del cupo antes de gastar: si no hay cupo, no hay llamada.
    limites.reservar_llamada(settings, ip=contexto.ip_actual())

    # Ningún llamador puede superar el tope configurado de salida.
    tope = int(settings.llm_max_tokens)
    payload = dict(payload)
    try:
        pedido = int(payload.get("max_tokens") or tope)
    except (TypeError, ValueError):
        pedido = tope
    payload["max_tokens"] = min(pedido, tope)

    peticion = Request(
        _endpoint(settings),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    plazo = float(timeout if timeout is not None else settings.llm_timeout_s)
    try:
        with urlopen(peticion, timeout=plazo) as respuesta:
            sobre = json.loads(respuesta.read())
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise LlmUpstreamError(
            "La consulta al proveedor no respondió o devolvió datos ilegibles."
        ) from exc
    if not isinstance(sobre, dict):
        raise LlmUpstreamError("La respuesta del proveedor no es un objeto JSON.")
    return sobre


def contenido(sobre: dict[str, Any]) -> str:
    """Extrae el mensaje del asistente del sobre de Chat Completions."""
    try:
        texto = sobre["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmUpstreamError(
            "La respuesta del proveedor no trae un mensaje utilizable."
        ) from exc
    if not isinstance(texto, str) or not texto.strip():
        raise LlmUpstreamError("La respuesta del proveedor llegó vacía.")
    return texto


def uso(sobre: dict[str, Any]) -> dict[str, int] | None:
    """Solo los contadores de tokens, para poder informar del consumo."""
    datos = sobre.get("usage")
    if not isinstance(datos, dict):
        return None
    limpio = {
        clave: valor
        for clave, valor in (
            ("prompt_tokens", datos.get("prompt_tokens")),
            ("completion_tokens", datos.get("completion_tokens")),
            ("total_tokens", datos.get("total_tokens")),
        )
        if isinstance(valor, int) and not isinstance(valor, bool)
    }
    return limpio or None


# ------------------------------------------------------------- validación común
def texto(valor: Any, *, campo: str, maximo: int = 600) -> str:
    """Texto de una línea, sin espacios sobrantes y con tope."""
    if not isinstance(valor, str):
        raise LlmRespuestaInvalida(f"El campo «{campo}» debe ser texto.")
    limpio = " ".join(valor.split())
    if not limpio:
        raise LlmRespuestaInvalida(f"El campo «{campo}» está vacío.")
    if len(limpio) > maximo:
        raise LlmRespuestaInvalida(f"El campo «{campo}» supera {maximo} caracteres.")
    return limpio


def lista(valor: Any, *, campo: str, maximo: int = 6, largo: int = 300) -> list[str]:
    if not isinstance(valor, list) or not valor:
        raise LlmRespuestaInvalida(f"El campo «{campo}» debe ser una lista no vacía.")
    if len(valor) > maximo:
        raise LlmRespuestaInvalida(
            f"El campo «{campo}» admite como máximo {maximo} elementos."
        )
    return [texto(item, campo=f"{campo}[{i}]", maximo=largo) for i, item in enumerate(valor)]


def objeto(bruto: str) -> dict[str, Any]:
    """JSON de objeto, o error de respuesta inválida."""
    try:
        datos = json.loads(bruto)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LlmRespuestaInvalida("El modelo no devolvió un JSON válido.") from exc
    if not isinstance(datos, dict):
        raise LlmRespuestaInvalida("El modelo no devolvió un objeto JSON.")
    return datos


def recortar(datos: Any, *, maximo: int = MAXIMO_CONTEXTO) -> str:
    """Serializa el contexto y lo acota a `maximo` caracteres, conservando JSON.

    Si hay que cortar, devuelve un objeto con la marca `__recortado__` y el
    prefijo del contexto como texto: el modelo sabe que la entrada va incompleta
    y la petición nunca supera el tope pactado.
    """
    crudo = json.dumps(datos, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(crudo) <= maximo:
        return crudo

    def envolver(fragmento: str) -> str:
        return json.dumps(
            {"__recortado__": True, "contexto_parcial": fragmento},
            ensure_ascii=False,
        )

    corte = max(0, len(crudo) - 1)
    salida = envolver(crudo[:corte])
    while corte > 0 and len(salida) > maximo:
        corte = max(0, corte - max(32, corte // 10))
        salida = envolver(crudo[:corte])
    return salida if len(salida) <= maximo or corte == 0 else envolver("")


def solicitar_json(
    settings: Settings,
    *,
    clave_limite: str,
    sistema: str,
    datos: Any,
    validar: Callable[[dict[str, Any]], dict[str, Any]],
    max_tokens: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pide un JSON al modelo, lo valida con `validar` y devuelve (salida, sobre).

    `validar` recibe el objeto decodificado y devuelve el diccionario ya limpio
    que verá la interfaz, o lanza `LlmRespuestaInvalida`.
    """
    if not configurado(settings):
        raise LlmNoConfigurado(
            "La consulta a OpenAI no está configurada en este despliegue."
        )
    consumir(settings, clave_limite)
    payload = {
        "model": str(settings.llm_model),
        "temperature": 0,
        "max_tokens": int(max_tokens or settings.llm_max_tokens),
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": f"{sistema}\nResponde en español."},
            {"role": "user", "content": recortar(datos)},
        ],
    }
    sobre = post_chat(settings, payload)
    datos_modelo = objeto(contenido(sobre))
    return validar(datos_modelo), sobre
