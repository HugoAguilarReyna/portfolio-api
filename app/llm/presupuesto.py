"""Presupuesto monetario de una corrida real contra el proveedor LLM.

El transporte compartido (`app.llm.cliente.post_chat`) consulta este módulo antes
de cada POST. La reserva se calcula con una cota superior deliberadamente
conservadora:

* entrada: todos los bytes UTF-8 del payload serializado (mensajes, esquemas de
  herramientas, modelo y parámetros); un token BPE nunca ocupa menos de un byte;
* salida: el `max_tokens` efectivo que ya recortó el transporte.

Cada llamada, incluidos los reintentos que haga el bucle del agente, pasa por la
misma reserva. Si el proveedor responde con `usage`, la reserva se concilia con
el consumo reportado y se libera lo no gastado. Si la solicitud falla de forma
ambigua (timeout, 5xx, respuesta ilegible o sin `usage`), la reserva **no se
libera**: pudo cobrarse y el siguiente paso no debe disponer de ese dinero.

Solo se admiten tarifas verificadas en el repositorio. Un modelo sin tarifa
conocida hace fallar la reserva antes de tocar la red (cero solicitudes).
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

#: USD por millón de tokens: (entrada, salida).
Tarifas = Mapping[str, tuple[float, float]]

_ACTIVO: ContextVar["PresupuestoMonetario | None"] = ContextVar(
    "llm_presupuesto_monetario", default=None
)
_EPSILON = 1e-12


class TarifaDesconocida(ValueError):
    """El modelo no tiene una tarifa verificada; no se puede reservar."""


class PresupuestoAgotado(RuntimeError):
    """La cota de la siguiente solicitud no cabe en el saldo disponible."""

    def __init__(self, mensaje: str, *, solicitado_usd: float, disponible_usd: float) -> None:
        super().__init__(mensaje)
        self.solicitado_usd = solicitado_usd
        self.disponible_usd = disponible_usd


class PresupuestoInvalido(ValueError):
    """El tope, la tarifa o el payload no permiten calcular una cota."""


@dataclass(frozen=True)
class Reserva:
    """Cota retenida para una solicitud concreta antes de llamar al proveedor."""

    id: int
    modelo: str
    entrada_bytes: int
    salida_tokens: int
    usd: float


def activo() -> "PresupuestoMonetario | None":
    """Presupuesto de la corrida actual, si hay uno activo en este contexto."""
    return _ACTIVO.get()


@contextmanager
def usar(gestor: "PresupuestoMonetario") -> Iterator["PresupuestoMonetario"]:
    """Activa el presupuesto para las solicitudes del contexto actual."""
    token = _ACTIVO.set(gestor)
    try:
        yield gestor
    finally:
        _ACTIVO.reset(token)


def _entero_no_negativo(valor: Any) -> int | None:
    if isinstance(valor, int) and not isinstance(valor, bool) and valor >= 0:
        return valor
    return None


class PresupuestoMonetario:
    """Contabilidad de reservas y consumo para una corrida acotada en USD."""

    def __init__(self, tope_usd: float, tarifas: Tarifas) -> None:
        if not (tope_usd > 0):
            raise PresupuestoInvalido("El tope de costo debe ser mayor que cero.")
        limpias: dict[str, tuple[float, float]] = {}
        for modelo, tarifa in tarifas.items():
            try:
                entrada, salida = (float(tarifa[0]), float(tarifa[1]))
            except (TypeError, ValueError, IndexError) as exc:
                raise PresupuestoInvalido(
                    f"La tarifa de «{modelo}» no tiene el formato (entrada, salida)."
                ) from exc
            if entrada < 0 or salida < 0 or not (entrada or salida):
                raise PresupuestoInvalido(f"La tarifa de «{modelo}» no es válida.")
            limpias[str(modelo)] = (entrada, salida)
        self.tope_usd = float(tope_usd)
        self.tarifas = limpias
        self._gastado_usd = 0.0
        self._reservado_usd = 0.0
        self._llamadas = 0
        self._conciliadas = 0
        self._fallidas = 0
        self._sin_uso = 0
        self._canceladas = 0
        self._excedido = False
        self._siguiente_id = 1
        self._pendientes: dict[int, Reserva] = {}
        self._candado = threading.RLock()

    @classmethod
    def para_modelo(
        cls, *, tope_usd: float, modelo: str, tarifas: Tarifas
    ) -> "PresupuestoMonetario":
        """Construye el gestor y falla cerrado si el modelo no está tarifado."""
        gestor = cls(tope_usd, tarifas)
        gestor.validar_tarifa(modelo)
        return gestor

    def validar_tarifa(self, modelo: str) -> tuple[float, float]:
        tarifa = self.tarifas.get(str(modelo))
        if tarifa is None:
            raise TarifaDesconocida(
                f"El modelo «{modelo}» no tiene tarifa verificada en el repositorio; "
                "no se hará ninguna solicitud al proveedor."
            )
        return tarifa

    @property
    def gastado_usd(self) -> float:
        with self._candado:
            return self._gastado_usd

    @property
    def reservado_usd(self) -> float:
        with self._candado:
            return self._reservado_usd

    @property
    def disponible_usd(self) -> float:
        with self._candado:
            return self._disponible_sin_candado()

    def _disponible_sin_candado(self) -> float:
        return max(0.0, self.tope_usd - self._gastado_usd - self._reservado_usd)

    def _tarifa_de(self, modelo: str) -> tuple[float, float]:
        return self.validar_tarifa(modelo)

    def cota_usd(self, payload: Mapping[str, Any]) -> float:
        """Cota superior en USD de una solicitud (sin retenerla todavía)."""
        return self._cota(payload)[2]

    def _cota(self, payload: Mapping[str, Any]) -> tuple[int, int, float]:
        modelo = str(payload.get("model") or "")
        entrada_tarifa, salida_tarifa = self._tarifa_de(modelo)
        salida = _entero_no_negativo(payload.get("max_tokens"))
        if salida is None:
            raise PresupuestoInvalido(
                "La solicitud no declara un máximo de tokens de salida; no se puede acotar."
            )
        try:
            serializado = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise PresupuestoInvalido("La solicitud no se puede serializar para acotarla.") from exc
        entrada_bytes = len(serializado)
        usd = entrada_bytes * entrada_tarifa / 1e6 + salida * salida_tarifa / 1e6
        return entrada_bytes, salida, usd

    def reservar(self, payload: Mapping[str, Any]) -> Reserva:
        """Retiene una cota antes de la solicitud o falla sin tocar la red."""
        entrada_bytes, salida, usd = self._cota(payload)
        modelo = str(payload.get("model") or "")
        with self._candado:
            disponible = self._disponible_sin_candado()
            if self._excedido or usd > disponible + _EPSILON:
                raise PresupuestoAgotado(
                    "La cota de la siguiente solicitud no cabe en el saldo restante; "
                    "no se llamó al proveedor.",
                    solicitado_usd=usd,
                    disponible_usd=disponible,
                )
            reserva = Reserva(
                id=self._siguiente_id,
                modelo=modelo,
                entrada_bytes=entrada_bytes,
                salida_tokens=salida,
                usd=usd,
            )
            self._siguiente_id += 1
            self._pendientes[reserva.id] = reserva
            self._reservado_usd += usd
            self._llamadas += 1
            return reserva

    def _consumir_reserva(self, reserva: Reserva) -> None:
        if self._pendientes.pop(reserva.id, None) is None:
            raise PresupuestoInvalido("La reserva ya fue conciliada o no existe.")
        self._reservado_usd -= reserva.usd
        if self._reservado_usd < 0 and abs(self._reservado_usd) < _EPSILON:
            self._reservado_usd = 0.0

    def _costo_reportado(self, reserva: Reserva, sobre: Any) -> float | None:
        uso = sobre.get("usage") if isinstance(sobre, dict) else None
        if not isinstance(uso, dict):
            return None
        entrada_tarifa, salida_tarifa = self._tarifa_de(reserva.modelo)
        prompt = _entero_no_negativo(uso.get("prompt_tokens"))
        completion = _entero_no_negativo(uso.get("completion_tokens"))
        if prompt is not None or completion is not None:
            # Si el proveedor solo informa una parte, la otra se mantiene en su
            # cota superior: nunca se libera dinero que no se pueda justificar.
            entrada = prompt if prompt is not None else reserva.entrada_bytes
            salida = completion if completion is not None else reserva.salida_tokens
            return entrada * entrada_tarifa / 1e6 + salida * salida_tarifa / 1e6
        total = _entero_no_negativo(uso.get("total_tokens"))
        if total is not None:
            # Sin desglose se aplica la tarifa más cara a todos los tokens.
            return total * max(entrada_tarifa, salida_tarifa) / 1e6
        return None

    def conciliar(self, reserva: Reserva, sobre: Any) -> None:
        """Ajusta la reserva al `usage` reportado; sin uso, la conserva."""
        with self._candado:
            self._consumir_reserva(reserva)
            costo = self._costo_reportado(reserva, sobre)
            if costo is None:
                self._gastado_usd += reserva.usd
                self._sin_uso += 1
            else:
                self._gastado_usd += max(0.0, costo)
                self._conciliadas += 1
            if self._gastado_usd > self.tope_usd + _EPSILON:
                self._excedido = True

    def fallar(self, reserva: Reserva) -> None:
        """Fallo ambiguo: la solicitud pudo cobrarse, así que se retiene la cota."""
        with self._candado:
            self._consumir_reserva(reserva)
            self._gastado_usd += reserva.usd
            self._fallidas += 1
            self._sin_uso += 1
            if self._gastado_usd > self.tope_usd + _EPSILON:
                self._excedido = True

    def cancelar(self, reserva: Reserva) -> None:
        """Libera una reserva cuando es seguro que no hubo solicitud al proveedor."""
        with self._candado:
            self._consumir_reserva(reserva)
            self._canceladas += 1

    def resumen(self) -> dict[str, Any]:
        with self._candado:
            return {
                "tope_usd": round(self.tope_usd, 9),
                "gastado_usd": round(self._gastado_usd, 9),
                "reservado_usd": round(self._reservado_usd, 9),
                "disponible_usd": round(self._disponible_sin_candado(), 9),
                "llamadas_facturables": self._llamadas,
                "conciliadas": self._conciliadas,
                "fallidas": self._fallidas,
                "sin_uso_reportado": self._sin_uso,
                "canceladas_sin_solicitud": self._canceladas,
                "excedido": self._excedido,
                "tarifas_usd_por_millon": {
                    modelo: {"entrada": entrada, "salida": salida}
                    for modelo, (entrada, salida) in sorted(self.tarifas.items())
                },
            }
