"""Telemetría anónima del portafolio sobre MongoDB.

Diseño:

* **Un documento global** (`_id: "global"`) en `metricas_visitas` con
  ``visitas_totales``, ``visitantes_unicos``, ``proyectos`` y ``areas``. Todos los
  incrementos son atómicos (``$inc``): sirven para varios workers y sobreviven a
  los reinicios.
* **Una huella por visitante y día** (`_id: "v:<AAAA-MM-DD>:<hmac>"`) para saber
  si es la primera visita del día. La huella es
  ``HMAC-SHA256(LLM_CUOTA_HMAC_KEY, ip|user-agent recortado|día)`` truncado a 32
  hex: sin la clave no se puede reconstruir la IP. El documento caduca solo a las
  48 horas con un índice TTL sobre ``expira_en``.
* **Sin IP ni User-Agent en claro**: no se guardan, no se registran en logs y no
  se usan cookies. El `User-Agent` solo sirve, en memoria, para descartar bots y
  como entrada del HMAC.
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
from app.db.mongo import METRICAS_VISITAS
from app.llm import limites

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

#: Entradas devueltas en `top_proyectos` (ranking estable por vistas y luego id).
MAXIMO_TOP = 10


def es_bot(user_agent: str | None) -> bool:
    """True para agentes vacíos, bots, rastreadores y monitoreo.

    Se aplica a todas las rutas de telemetría: un bot no infla ni las visitas ni
    el ranking de proyectos.
    """
    agente = (user_agent or "").strip().lower()
    if not agente:
        return True
    return any(patron in agente for patron in PATRONES_BOT)


def _dia_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _expira() -> datetime:
    return _ahora() + timedelta(hours=HORAS_HUELLA)


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
        self.settings = settings

    # ------------------------------------------------------------- lectura
    def _global(self) -> dict[str, Any]:
        documento = self.coleccion.find_one({"_id": DOCUMENTO_GLOBAL})
        return documento if isinstance(documento, dict) else {}

    def estado(self) -> dict[str, Any]:
        """Estado actual sin modificar nada (también para bots)."""
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
        user_agent: str | None,
    ) -> dict[str, Any]:
        """Incrementa la vista de un proyecto (y de su área, si se indica).

        Los identificadores llegan validados por Pydantic (alfanumérico y guiones),
        así que no pueden contener puntos ni operadores de MongoDB en las rutas
        ``proyectos.<id>`` / ``areas.<id>``. Los bots no incrementan nada.
        """
        if es_bot(user_agent):
            documento = self._global()
            proyectos = documento.get("proyectos") or {}
            return {
                "ok": True,
                "proyecto_id": proyecto_id,
                "vistas_proyecto": int(proyectos.get(proyecto_id, 0)),
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
