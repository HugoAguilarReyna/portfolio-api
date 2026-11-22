"""Cuotas compartidas de las consultas LLM (1 por IP/día, 10 globales/día).

Todo se contabiliza en la **misma base MongoDB de la aplicación**, en la base y
colección dedicadas `portfolio_guard.llm_limites`, con incrementos atómicos
(`find_one_and_update` + `upsert`): los contadores valen para todos los workers y
sobreviven a los reinicios.

Decisiones de diseño:

* **Reserva antes de llamar**: el cupo se toma en `cliente.post_chat`, el único
  transporte que usan las seis demos. Si la reserva falla, no hay llamada.
* **Una llamada que falla sigue contando**: el proveedor pudo procesarla (y
  facturarla) antes de agotar el tiempo de espera; el objetivo es proteger el
  gasto, no premiar los fallos. Está documentado y probado.
* **Sin IP en claro**: el identificador es `HMAC-SHA256(LLM_CUOTA_HMAC_KEY,
  ip|día)` truncado a 32 hex. La clave vive solo en el entorno del `api`; si
  falta, las rutas LLM **fallan cerrado** (503) y el resto del sitio sigue igual.
* **Días UTC**: la cuota se reinicia a medianoche UTC.
* **Intervalo mínimo** entre consultas del mismo visitante (10 minutos). Con una
  única consulta diaria por IP, el límite diario ya es más estricto; el intervalo
  protege además si algún día se relaja ese tope.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address, ip_network
from typing import Any, Mapping

from pymongo.errors import PyMongoError

from app.config import Settings

CABECERAS_IP = ("x-real-ip", "x-forwarded-for")
MINIMO_CLAVE_HEX = 32


class LimitesNoConfigurados(RuntimeError):
    """Falta la clave HMAC de cuotas; sin ella no se puede identificar visitantes."""


class LimitesNoDisponibles(RuntimeError):
    """El almacén de cuotas no responde: las rutas LLM fallan cerrado."""


class CuotaExcedida(RuntimeError):
    """Se alcanzó una cuota; `reintentar_en` indica cuántos segundos esperar."""

    def __init__(self, mensaje: str, *, reintentar_en: int) -> None:
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
    if not host:
        return False
    try:
        direccion = ip_address(host)
    except ValueError:
        return False
    return any(direccion in red for red in _redes(settings.llm_proxies_confiables))


def ip_real(host_conexion: str | None, cabeceras: Mapping[str, str], settings: Settings) -> str:
    """IP del visitante; solo se aceptan cabeceras de proxies declarados.

    Traefik/nginx añaden `X-Real-IP`/`X-Forwarded-For`. Si la conexión llega de
    una red confiable se toma la primera IP de esas cabeceras; si no, se usa la IP
    de la conexión y se ignora lo que envíe el cliente (no puede falsificarla).
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


def clave_hmac(settings: Settings) -> str:
    clave = (settings.llm_cuota_hmac_key or "").strip()
    if len(clave) < MINIMO_CLAVE_HEX:
        raise LimitesNoConfigurados(
            "Las cuotas LLM no están configuradas (falta LLM_CUOTA_HMAC_KEY). "
            "El servicio no hará consultas al proveedor hasta configurarla."
        )
    return clave


def identificador(ip: str, dia: str, settings: Settings) -> str:
    """HMAC con la clave del despliegue: sin ella no se puede reconstruir la IP."""
    mensaje = f"{ip}|{dia}".encode("utf-8")
    return hmac.new(clave_hmac(settings).encode("utf-8"), mensaje, hashlib.sha256).hexdigest()[:32]


def _dia_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ahora_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _segundos_hasta_medianoche_utc() -> int:
    ahora = datetime.now(timezone.utc)
    manana = (ahora + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((manana - ahora).total_seconds()))


def _expira(minutos: int = 60 * 49) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutos)


class Limitador:
    def __init__(self, db: Any, settings: Settings) -> None:
        self.coleccion = db[settings.llm_limites_coleccion]
        self.settings = settings

    def _incrementar(self, clave: str, *, campos: dict[str, int], conjunto: dict | None = None):
        try:
            documento = self.coleccion.find_one_and_update(
                {"_id": clave},
                {"$inc": campos, "$set": conjunto or {}, "$setOnInsert": {"expira_en": _expira()}},
                upsert=True,
                return_document=True,
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

    def _ultima(self, clave: str) -> int | None:
        try:
            documento = self.coleccion.find_one({"_id": clave})
        except PyMongoError as exc:
            raise LimitesNoDisponibles(
                "No se pudo comprobar la cuota de consultas; inténtalo más tarde."
            ) from exc
        if not isinstance(documento, dict):
            return None
        valor = documento.get("ultima")
        return int(valor) if isinstance(valor, int) else None

    def reservar(self, *, ip: str, costo_tokens: int | None = None) -> dict[str, int]:
        """Reserva una consulta; lanza `CuotaExcedida` si no hay cupo.

        Orden: intervalo mínimo del visitante, cupo diario por IP, cupo global de
        solicitudes y tope global de tokens. Todos los contadores se incrementan
        de forma atómica antes de devolver el permiso.
        """
        dia = _dia_utc()
        clave = identificador(ip, dia, self.settings)
        ahora = _ahora_ms()

        ultima = self._ultima(f"ip:{clave}:{dia}")
        if ultima is not None:
            transcurrido = (ahora - ultima) / 1000
            if transcurrido < self.settings.llm_intervalo_minimo_s:
                espera = max(1, int(round(self.settings.llm_intervalo_minimo_s - transcurrido)))
                raise CuotaExcedida(
                    "Acabas de hacer una consulta: espera unos minutos antes de "
                    "volver a intentarlo.",
                    reintentar_en=espera,
                )

        por_ip = self._incrementar(f"ip:{clave}:{dia}", campos={"n": 1}, conjunto={"ultima": ahora})
        if int(por_ip.get("n", 0)) > int(self.settings.llm_max_solicitudes_por_ip_dia):
            raise CuotaExcedida(
                "Este visitante ya usó su consulta de hoy; el cupo se renueva a "
                "medianoche UTC.",
                reintentar_en=_segundos_hasta_medianoche_utc(),
            )

        costo = int(
            costo_tokens if costo_tokens is not None else self.settings.llm_max_tokens
        )
        global_dia = self._incrementar(
            f"total:{dia}", campos={"n": 1, "tokens": costo}, conjunto=None
        )
        if int(global_dia.get("n", 0)) > int(self.settings.llm_max_solicitudes_dia):
            raise CuotaExcedida(
                "El portafolio agotó sus consultas de hoy; el cupo se renueva a "
                "medianoche UTC.",
                reintentar_en=_segundos_hasta_medianoche_utc(),
            )
        if int(global_dia.get("tokens", 0)) > int(self.settings.llm_max_tokens_dia):
            raise CuotaExcedida(
                "El portafolio agotó su presupuesto diario de tokens; vuelve a "
                "medianoche UTC.",
                reintentar_en=_segundos_hasta_medianoche_utc(),
            )
        return {"visitante_del_dia": int(por_ip.get("n", 0)), "global_del_dia": int(global_dia.get("n", 0))}

    def estado(self) -> dict[str, int]:
        dia = _dia_utc()
        documento = self.coleccion.find_one({"_id": f"total:{dia}"}) or {}
        return {
            "solicitudes_hoy": int(documento.get("n", 0)),
            "tokens_reservados_hoy": int(documento.get("tokens", 0)),
            "maximo_por_ip_dia": int(self.settings.llm_max_solicitudes_por_ip_dia),
            "maximo_global_dia": int(self.settings.llm_max_solicitudes_dia),
            "maximo_tokens_dia": int(self.settings.llm_max_tokens_dia),
            "intervalo_minimo_s": int(self.settings.llm_intervalo_minimo_s),
            "tokens_por_consulta": int(self.settings.llm_max_tokens),
            "tokens_por_run_agente": int(self.settings.llm_agente_presupuesto_tokens),
            "dia_utc": dia,
        }


def limitador_para(db: Any, settings: Settings) -> Limitador:
    return Limitador(db, settings)


def reservar_llamada(
    settings: Settings, *, ip: str | None, costo_tokens: int | None = None
) -> dict[str, int]:
    """Punto único de reserva, invocado por el cliente antes de cada llamada.

    `costo_tokens` permite reservar el presupuesto real de una operación (por
    ejemplo el run completo del agente) en lugar del techo de una sola consulta.
    """
    clave_hmac(settings)  # falla cerrado si no hay clave configurada
    from app.db.mongo import get_database

    try:
        base = get_database().db.client[settings.llm_limites_db]
    except Exception as exc:  # noqa: BLE001 - sin almacén no se llama al proveedor
        raise LimitesNoDisponibles(
            "No se pudo comprobar la cuota de consultas; inténtalo más tarde."
        ) from exc
    return limitador_para(base, settings).reservar(
        ip=ip or "desconocida", costo_tokens=costo_tokens
    )


def asegurar_indices(db: Any, settings: Settings) -> None:
    try:
        db[settings.llm_limites_coleccion].create_index(
            "expira_en", expireAfterSeconds=0, name="ttl_cuotas"
        )
    except PyMongoError:
        return


def reservar_run(settings: Settings, *, ip: str | None) -> dict[str, int]:
    """Reserva única del run del agente: 1 solicitud y su presupuesto de tokens.

    El bucle de hasta 6 pasos no vuelve a reservar (`contexto.reserva_de_run`),
    así que un run consume una sola solicitud del cupo diario por IP y del cupo
    global, y reserva el presupuesto completo de tokens del run.
    """
    return reservar_llamada(
        settings,
        ip=ip,
        costo_tokens=int(settings.llm_agente_presupuesto_tokens),
    )
