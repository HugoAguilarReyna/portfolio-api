"""Cuotas de uso del proveedor LLM compartidas entre workers.

El contador en memoria de `cliente.py` protege a un proceso; esto protege a la
API pública: varias instancias/workers incrementan los mismos documentos en la
base de datos de la aplicación, de forma **atómica** (`find_one_and_update` con
`upsert`), así que un visitante no puede consumir la cuota de los demás.

Decisiones:

* **Almacén**: la base de datos de la aplicación (MongoDB), en una base y
  colección dedicadas (`llm_limites`). Es privada (sin puerto público), persistente
  y ya está en el despliegue: no se usa el Redis global de Dokploy ni se añade un
  servicio con otro ciclo de vida. La interfaz está aislada aquí por si se quisiera
  cambiar por Redis.
* **Claves derivadas**: nunca se guarda ni se registra la IP en claro. La clave es
  `HMAC-SHA256(secret, ip|día)` truncada, con `secret` = la clave del proveedor
  (presente solo en el entorno del `api`) y el día dentro del mensaje: las claves
  caducan solas y no se pueden revertir sin el secreto.
* **Falla cerrado**: si la base de cuotas no responde, las rutas LLM devuelven 503.
  Las rutas que no usan LLM no pasan por aquí.
* **Presupuesto**: se reserva el techo (`llm_max_tokens`) antes de llamar, no el
  consumo real; así el tope diario de tokens no se puede rebasar. Puede cortar
  antes de agotar el presupuesto real, nunca después.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address, ip_network
from typing import Any, Iterable, Mapping

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from app.config import Settings

#: Cabeceras que solo se aceptan cuando la conexión viene de un proxy confiable.
CABECERAS_IP = ("x-real-ip", "x-forwarded-for")


class LimitesNoDisponibles(RuntimeError):
    """El almacén de cuotas no responde: las rutas LLM fallan cerrado."""


class CuotaExcedida(RuntimeError):
    """Se alcanzó un límite de uso; `reintentar_en` indica cuándo reintentar."""

    def __init__(self, mensaje: str, *, reintentar_en: int = 60) -> None:
        super().__init__(mensaje)
        self.reintentar_en = max(1, int(reintentar_en))


def _redes(valor: str) -> list[Any]:
    redes = []
    for trozo in (valor or "").split(","):
        limpio = trozo.strip()
        if not limpio:
            continue
        try:
            redes.append(ip_network(limpio, strict=False))
        except ValueError:
            continue
    return redes


def es_proxy_confiable(host: str | None, settings: Settings) -> bool:
    """Solo las redes declaradas pueden aportar cabeceras de IP."""
    if not host:
        return False
    try:
        direccion = ip_address(host)
    except ValueError:
        return False
    return any(direccion in red for red in _redes(settings.llm_proxies_confiables))


def ip_real(host_conexion: str | None, cabeceras: Mapping[str, str], settings: Settings) -> str:
    """IP del visitante: la de la conexión, salvo que venga de un proxy confiable.

    `X-Real-IP` lo escribe el nginx del despliegue con `$remote_addr`; solo se usa
    si el par de la conexión está en las redes confiables. `X-Forwarded-For` se
    ignora salvo en ese mismo caso y se toma su primer valor.
    """
    if not es_proxy_confiable(host_conexion, settings):
        return host_conexion or "desconocida"
    for cabecera in CABECERAS_IP:
        valor = cabeceras.get(cabecera)
        if not valor:
            continue
        primero = valor.split(",")[0].strip()
        try:
            return str(ip_address(primero))
        except ValueError:
            continue
    return host_conexion or "desconocida"


def clave_ip(ip: str, dia: str, settings: Settings) -> str:
    """Clave derivada con vencimiento diario; no se puede revertir sin el secreto."""
    secreto = (settings.llm_api_key or settings.app_name or "sin-proveedor").encode("utf-8")
    return hmac.new(secreto, f"{ip}|{dia}".encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def _dia_actual() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _minuto_actual() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M")


def _expira(dias: int = 2) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=dias)


def _expira_minutos(minutos: int = 5) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutos)


class Limitador:
    """Contadores diarios y por minuto, atómicos en la colección dedicada."""

    def __init__(self, db: Any, settings: Settings) -> None:
        self.coleccion = db[settings.llm_limites_coleccion]

    def _incrementar(self, clave: str, *, campos: dict[str, int], expira: datetime) -> dict[str, Any]:
        try:
            documento = self.coleccion.find_one_and_update(
                {"_id": clave},
                {
                    "$inc": campos,
                    "$setOnInsert": {"expira_en": expira},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except PyMongoError as exc:
            raise LimitesNoDisponibles(
                "No se pudo comprobar la cuota de consultas; inténtalo más tarde."
            ) from exc
        if not isinstance(documento, dict):
            raise LimitesNoDisponibles(
                "No se pudo comprobar la cuota de consultas; inténtalo más tarde."
            )
        return documento

    def reservar(
        self,
        *,
        clave_visitante: str,
        settings: Settings,
        costo_tokens: int,
    ) -> dict[str, int]:
        """Reserva una solicitud y su techo de tokens; lanza si supera cualquier tope."""
        dia = _dia_actual()
        minuto = _minuto_actual()

        por_minuto = self._incrementar(
            f"m:{clave_visitante}:{minuto}",
            campos={"n": 1},
            expira=_expira_minutos(5),
        )
        if int(por_minuto.get("n", 0)) > int(settings.llm_consultas_por_minuto):
            raise CuotaExcedida(
                "Demasiadas consultas seguidas desde este visitante; espera un minuto.",
                reintentar_en=60,
            )

        por_ip = self._incrementar(
            f"ip:{clave_visitante}:{dia}",
            campos={"n": 1},
            expira=_expira(2),
        )
        if int(por_ip.get("n", 0)) > int(settings.llm_max_solicitudes_por_ip_dia):
            raise CuotaExcedida(
                "Este visitante alcanzó el máximo de consultas por día.", reintentar_en=3600
            )

        global_dia = self._incrementar(
            f"total:{dia}",
            campos={"n": 1, "tokens": costo_tokens},
            expira=_expira(2),
        )
        if int(global_dia.get("n", 0)) > int(settings.llm_max_solicitudes_dia):
            raise CuotaExcedida(
                "La API alcanzó su máximo diario de consultas; vuelve mañana.",
                reintentar_en=3600,
            )
        if int(global_dia.get("tokens", 0)) > int(settings.llm_max_tokens_dia):
            raise CuotaExcedida(
                "La API alcanzó su presupuesto diario de tokens; vuelve mañana.",
                reintentar_en=3600,
            )
        return {
            "visitante_del_dia": int(por_ip.get("n", 0)),
            "global_del_dia": int(global_dia.get("n", 0)),
        }

    def estado(self) -> dict[str, int]:
        """Uso de hoy, para `/health` (solo contadores, sin claves de visitante)."""
        dia = _dia_actual()
        documento = self.coleccion.find_one({"_id": f"total:{dia}"}) or {}
        return {
            "solicitudes_hoy": int(documento.get("n", 0)),
            "tokens_reservados_hoy": int(documento.get("tokens", 0)),
        }


def limitador_para(db: Any, settings: Settings) -> Limitador:
    return Limitador(db, settings)


def asegurar_indices(db: Any, settings: Settings) -> None:
    """Índice TTL: los contadores caducan solos en el almacén."""
    try:
        db[settings.llm_limites_coleccion].create_index(
            "expira_en", expireAfterSeconds=0, name="ttl_cuotas"
        )
    except PyMongoError:
        # Un fallo al crear el índice no debe tumbar el arranque: los documentos
        # llevan `expira_en` y se limpian igualmente en la siguiente comprobación.
        return


def reservar_para_peticion(
    *,
    host_conexion: str | None,
    cabeceras: Mapping[str, str],
    db: Any,
    settings: Settings,
) -> dict[str, int]:
    """Punto de entrada de las rutas: IP real → clave derivada → reserva atómica."""
    ip = ip_real(host_conexion, cabeceras, settings)
    clave = clave_ip(ip, _dia_actual(), settings)
    return limitador_para(db, settings).reservar(
        clave_visitante=clave,
        settings=settings,
        costo_tokens=int(settings.llm_max_tokens),
    )
