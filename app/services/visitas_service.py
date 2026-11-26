"""Telemetría anónima del portafolio sobre MongoDB.

Diseño:

* **Un documento global** (`_id: "global"`) en `metricas_visitas` con
  ``visitas_totales``, ``visitantes_unicos``, ``proyectos`` y ``areas``. Todos los
  incrementos son atómicos (``$inc``): sirven para varios workers y sobreviven a
  los reinicios. Sus claves solo pueden ser identificadores del catálogo cerrado.
* **Una huella por visitante y día** (`_id: "v:<AAAA-MM-DD>:<hmac>"`) para saber
  si es la primera visita del día y qué proyectos ya exploró. La huella es
  ``HMAC-SHA256(LLM_CUOTA_HMAC_KEY, ip|user-agent recortado|día)`` truncado a 32
  hex: sin la clave no se puede reconstruir la IP. El documento caduca solo a las
  48 horas con un índice TTL sobre ``expira_en``.
* **Una vista por visitante, día y proyecto**: la huella guarda un conjunto
  acotado de proyectos vistos (``$addToSet`` con tope configurable). Solo se
  incrementa el ranking cuando el proyecto es nuevo para esa huella ese día.
* **Solo cuenta quien se registró**: ``registrar_proyecto`` no hace upsert de la
  huella. Si ``POST /visitas/registrar`` no creó la huella del día, la vista se
  responde con el contador actual y no se incrementa nada; tampoco se crea estado
  nuevo por IP.
* **Catálogo cerrado**: ``app/data/catalogo_ids.json`` (exportado de
  ``dist/content.js`` del sitio) limita proyectos y áreas. Un identificador
  desconocido o un área que no corresponde al proyecto se rechaza con 422.
* **Frecuencia**: un contador TTL por huella y minuto limita las peticiones de
  escritura (30/min por omisión). Excedido, la ruta responde 429 con
  ``Retry-After``. El contador vive en ``metricas_visitas_limites`` y no guarda IP.
* **Sin IP ni User-Agent en claro**: no se guardan, no se registran en logs y no
  se usan cookies. El `User-Agent` solo sirve, en memoria, para descartar bots y
  como entrada del HMAC. El filtro por User-Agent es **de mejor esfuerzo**: un
  cliente que falsifique la cabecera puede colarse; el ranking mide
  **exploración anónima**, no analítica forense.
* **Bots y monitoreo**: un `User-Agent` vacío o con patrones conocidos no
  incrementa ningún contador; la respuesta informa ``contabilizada: false``.

La clave HMAC es la misma que protege las cuotas LLM. Si falta, registrar una
visita falla cerrado (503) en lugar de contar sin poder distinguir visitantes.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ReturnDocument

from app.config import Settings
from app.db.mongo import METRICAS_VISITAS, METRICAS_VISITAS_LIMITES
from app.llm import limites
from app.services.catalogo_ids import catalogo_ids

DOCUMENTO_GLOBAL = "global"

#: Patrones (en minúsculas) de agentes automáticos que no cuentan como visitas.
PATRONES_BOT = (
    "bot",
    "crawler",
    "spider",
    "curl",
    "wget",
    "python-requests",
    "healthcheck",
    "uptime",
    "monitoring",
)

#: La huella diaria vive dos días; el índice TTL de `expira_en` la elimina sola.
HORAS_HUELLA = 48

#: Los contadores de frecuencia viven dos horas (solo necesitan el minuto actual).
HORAS_LIMITE = 2

#: Entradas devueltas en `top_proyectos` (ranking estable por vistas y luego id).
MAXIMO_TOP = 10


class ProyectoDesconocido(ValueError):
    """El `proyecto_id` no está en el catálogo cerrado."""


class AreaDesconocida(ValueError):
    """El `area_id` no está en el catálogo cerrado."""


class AreaNoCorresponde(ValueError):
    """El área indicada no es la del proyecto en el catálogo."""


class LimiteFrecuencia(RuntimeError):
    """Se superó el límite por minuto; `reintentar_en` indica cuántos segundos."""

    def __init__(self, reintentar_en: int) -> None:
        super().__init__("Demasiadas peticiones de telemetría; espera un momento.")
        self.reintentar_en = max(1, int(reintentar_en))


def es_bot(user_agent: str | None) -> bool:
    """True para agentes vacíos, bots, rastreadores y monitoreo.

    Filtro **de mejor esfuerzo**: depende de una cabecera que el cliente controla.
    Se aplica a todas las rutas de telemetría para no inflar las visitas ni el
    ranking con agentes automáticos evidentes.
    """
    agente = (user_agent or "").strip().lower()
    if not agente:
        return True
    return any(patron in agente for patron in PATRONES_BOT)


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _dia_utc() -> str:
    return _ahora().strftime("%Y-%m-%d")


def _expira() -> datetime:
    return _ahora() + timedelta(hours=HORAS_HUELLA)


def _expira_limite() -> datetime:
    return _ahora() + timedelta(hours=HORAS_LIMITE)


def _segundos_hasta_siguiente_minuto() -> int:
    return max(1, 60 - _ahora().second)


def huella_diaria(
    ip: str,
    user_agent: str | None,
    settings: Settings,
    *,
    dia: str | None = None,
) -> str:
    """Huella anónima ``v:<AAAA-MM-DD>:<hmac>`` para la visita de hoy.

    El `User-Agent` se recorta a 120 caracteres y la IP nunca se guarda: el HMAC
    es irreversible sin ``LLM_CUOTA_HMAC_KEY``. La clave se valida con
    :func:`app.llm.limites.clave_hmac`, la misma que usan las cuotas LLM, y si
    falta se lanza ``LimitesNoConfigurados`` (la ruta responde 503).
    """
    clave = limites.clave_hmac(settings)
    fecha = dia or _dia_utc()
    agente = (user_agent or "")[:120]
    mensaje = f"{ip}|{agente}|{fecha}".encode("utf-8")
    resumen = hmac.new(clave.encode("utf-8"), mensaje, hashlib.sha256).hexdigest()[:32]
    return f"v:{fecha}:{resumen}"


class ServicioVisitas:
    """Contadores y ranking de la telemetría anónima."""

    def __init__(self, db: Any, settings: Settings) -> None:
        self.coleccion = db[METRICAS_VISITAS]
        self.limites = db[METRICAS_VISITAS_LIMITES]
        self.settings = settings

    # ------------------------------------------------------------- lectura
    def _global(self) -> dict[str, Any]:
        documento = self.coleccion.find_one({"_id": DOCUMENTO_GLOBAL})
        return documento if isinstance(documento, dict) else {}

    def _vistas(self, proyecto_id: str) -> int:
        proyectos = self._global().get("proyectos") or {}
        return int(proyectos.get(proyecto_id, 0))

    def estado(self) -> dict[str, Any]:
        """Estado actual sin modificar nada (también para bots).

        El ranking mide exploración anónima del sitio; no es analítica forense y
        no identifica visitantes.
        """
        documento = self._global()
        proyectos = documento.get("proyectos") or {}
        ranking = sorted(
            ((str(proyecto), int(vistas)) for proyecto, vistas in proyectos.items()),
            key=lambda par: (-par[1], par[0]),
        )[:MAXIMO_TOP]
        return {
            "visitas_totales": int(documento.get("visitas_totales", 0)),
            "visitantes_unicos": int(documento.get("visitantes_unicos", 0)),
            "top_proyectos": [
                {"proyecto_id": proyecto, "vistas": vistas}
                for proyecto, vistas in ranking
            ],
        }

    # ------------------------------------------------------------ escritura
    def _limitar(self, huella: str) -> None:
        """Cuenta la petición del minuto y lanza si supera el tope.

        La clave es ``rl:<huella>:<AAAA-MM-DDTHH:MM>``: no contiene IP, caduca por
        TTL y sirve para todos los workers. Se incrementa antes de decidir para
        que dos peticiones simultáneas del mismo visitante sumen.
        """
        minuto = _ahora().strftime("%Y-%m-%dT%H:%M")
        documento = self.limites.find_one_and_update(
            {"_id": f"rl:{huella}:{minuto}"},
            {
                "$inc": {"n": 1},
                "$setOnInsert": {"expira_en": _expira_limite()},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        usados = int(documento.get("n", 1)) if isinstance(documento, dict) else 1
        if usados > int(self.settings.telemetria_max_peticiones_minuto):
            raise LimiteFrecuencia(_segundos_hasta_siguiente_minuto())

    def registrar(self, *, ip: str, user_agent: str | None) -> dict[str, Any]:
        """Cuenta una visita y, si es la primera del día de esa huella, un único.

        La primera visita del día se detecta con el resultado atómico del upsert
        de la huella: ``upserted_id`` solo viene informado cuando el documento se
        creó en esta misma operación. El documento global se actualiza con
        ``find_one_and_update`` + ``$inc`` y devuelve los contadores resultantes.
        """
        if es_bot(user_agent):
            estado = self.estado()
            return {
                "visitas_totales": estado["visitas_totales"],
                "visitantes_unicos": estado["visitantes_unicos"],
                "contabilizada": False,
            }

        dia = _dia_utc()
        huella = huella_diaria(ip, user_agent, self.settings, dia=dia)
        self._limitar(huella)
        creada = self.coleccion.update_one(
            {"_id": huella},
            {
                "$setOnInsert": {
                    "dia": dia,
                    "expira_en": _expira(),
                }
            },
            upsert=True,
        )
        es_unico_hoy = creada.upserted_id is not None

        incrementos: dict[str, int] = {"visitas_totales": 1}
        if es_unico_hoy:
            incrementos["visitantes_unicos"] = 1
        documento = self.coleccion.find_one_and_update(
            {"_id": DOCUMENTO_GLOBAL},
            {"$inc": incrementos},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        documento = documento if isinstance(documento, dict) else {}
        return {
            "visitas_totales": int(documento.get("visitas_totales", 1)),
            "visitantes_unicos": int(documento.get("visitantes_unicos", 1 if es_unico_hoy else 0)),
            "contabilizada": True,
        }

    def registrar_proyecto(
        self,
        *,
        proyecto_id: str,
        area_id: str | None,
        ip: str,
        user_agent: str | None,
    ) -> dict[str, Any]:
        """Cuenta una exploración nueva por visita y día; devuelve el total.

        Reglas:

        * El proyecto y el área deben existir en el catálogo; el área, si se
          indica, debe ser la del proyecto. Cualquier fallo lanza un ``ValueError``
          que la ruta traduce a 422 y no toca ningún contador.
        * Los bots no incrementan nada.
        * Solo cuenta si ``/registrar`` ya creó la huella del día: el ``update_one``
          no hace upsert y el filtro exige la huella, el proyecto ausente en el
          conjunto y espacio en el tope.
        * El incremento global usa rutas ``proyectos.<id>`` / ``areas.<id>`` con
          identificadores del catálogo, así que el documento queda acotado.
        """
        catalogo = catalogo_ids()
        if proyecto_id not in catalogo.proyectos:
            raise ProyectoDesconocido(
                f"El proyecto {proyecto_id!r} no está en el catálogo del portafolio."
            )
        if area_id is not None:
            if area_id not in catalogo.areas:
                raise AreaDesconocida(
                    f"El área {area_id!r} no está en el catálogo del portafolio."
                )
            if catalogo.area_de(proyecto_id) != area_id:
                raise AreaNoCorresponde(
                    f"El proyecto {proyecto_id!r} pertenece al área "
                    f"{catalogo.area_de(proyecto_id)!r}, no a {area_id!r}."
                )

        if es_bot(user_agent):
            return {
                "ok": True,
                "proyecto_id": proyecto_id,
                "vistas_proyecto": self._vistas(proyecto_id),
            }

        huella = huella_diaria(ip, user_agent, self.settings)
        self._limitar(huella)
        tope = max(1, int(self.settings.telemetria_max_proyectos_huella))
        filtro: dict[str, Any] = {
            "_id": huella,
            "proyectos": {"$ne": proyecto_id},
            f"proyectos.{tope - 1}": {"$exists": False},
        }
        actualizada = self.coleccion.update_one(
            filtro, {"$addToSet": {"proyectos": proyecto_id}}
        )
        if getattr(actualizada, "modified_count", 0) != 1:
            # Sin huella (no registró su visita), proyecto ya contado hoy o tope.
            return {
                "ok": True,
                "proyecto_id": proyecto_id,
                "vistas_proyecto": self._vistas(proyecto_id),
            }

        incrementos: dict[str, int] = {f"proyectos.{proyecto_id}": 1}
        if area_id:
            incrementos[f"areas.{area_id}"] = 1
        documento = self.coleccion.find_one_and_update(
            {"_id": DOCUMENTO_GLOBAL},
            {"$inc": incrementos},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        documento = documento if isinstance(documento, dict) else {}
        proyectos = documento.get("proyectos") or {}
        return {
            "ok": True,
            "proyecto_id": proyecto_id,
            "vistas_proyecto": int(proyectos.get(proyecto_id, 1)),
        }


__all__ = [
    "AreaDesconocida",
    "AreaNoCorresponde",
    "LimiteFrecuencia",
    "ProyectoDesconocido",
    "ServicioVisitas",
    "es_bot",
    "huella_diaria",
]
