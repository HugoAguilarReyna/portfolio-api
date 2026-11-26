"""Catálogo cerrado de identificadores de la telemetría.

La fuente única es `dist/content.js` del sitio. El script
``tools/exportar-catalogo-ids.mjs`` (repo del sitio) genera
``app/data/catalogo_ids.json``; este módulo lo valida y lo expone como conjuntos
inmutables. Un proyecto o área que no esté en el catálogo se rechaza con 422 y no
se incrementa nada: sin esta lista, la telemetría aceptaría cualquier slug.

Sincronización (repetir cuando cambien las fichas del sitio)::

    cd portafolio-hugo-aguilar
    node tools/exportar-catalogo-ids.mjs \\
        --salida ../portfolio-api/app/data/catalogo_ids.json

El archivo se lee una vez por proceso (``lru_cache``); un despliegue nuevo recoge
los cambios. Si falta o está mal formado, las rutas de escritura fallan cerrado
(503) en lugar de aceptar identificadores arbitrarios.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

#: Mismo patrón que valida Pydantic en `app/models/visitas.py`.
PATRON_SLUG = re.compile(r"^[a-zA-Z0-9-]{2,64}$")

#: `app/data/catalogo_ids.json`, junto al paquete `app`.
RUTA_CATALOGO = Path(__file__).resolve().parents[1] / "data" / "catalogo_ids.json"


class CatalogoInvalido(RuntimeError):
    """El catálogo no existe, no es JSON o no cumple el esquema esperado."""


@dataclass(frozen=True)
class CatalogoIds:
    """Identificadores permitidos y la relación proyecto → área."""

    areas: frozenset[str]
    proyectos: frozenset[str]
    proyecto_area: dict[str, str]
    actualizado: str | None = None

    def area_de(self, proyecto_id: str) -> str | None:
        return self.proyecto_area.get(proyecto_id)


def _lista_de_ids(crudo: Any, nombre: str) -> frozenset[str]:
    if not isinstance(crudo, list) or not crudo:
        raise CatalogoInvalido(f"El catálogo debe incluir una lista no vacía '{nombre}'.")
    valores = frozenset(str(valor) for valor in crudo)
    if len(valores) != len(crudo):
        raise CatalogoInvalido(f"El catálogo repite identificadores en '{nombre}'.")
    for valor in valores:
        if not PATRON_SLUG.fullmatch(valor):
            raise CatalogoInvalido(f"Identificador inválido en '{nombre}': {valor!r}.")
    return valores


@lru_cache(maxsize=1)
def catalogo_ids() -> CatalogoIds:
    """Carga y valida el catálogo. Falla con `CatalogoInvalido` si no sirve."""
    try:
        crudo = json.loads(RUTA_CATALOGO.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CatalogoInvalido(
            f"No se pudo leer {RUTA_CATALOGO.name}: falta el catálogo de telemetría."
        ) from exc
    except json.JSONDecodeError as exc:
        raise CatalogoInvalido(
            f"{RUTA_CATALOGO.name} no contiene JSON válido."
        ) from exc
    if not isinstance(crudo, dict):
        raise CatalogoInvalido("El catálogo debe ser un objeto JSON.")

    areas = _lista_de_ids(crudo.get("areas"), "areas")
    proyectos = _lista_de_ids(crudo.get("proyectos"), "proyectos")
    mapa = crudo.get("proyecto_area")
    if not isinstance(mapa, dict) or set(mapa) != set(proyectos):
        raise CatalogoInvalido(
            "El catálogo debe mapear exactamente los proyectos en 'proyecto_area'."
        )
    for proyecto, area in mapa.items():
        if not isinstance(area, str) or area not in areas:
            raise CatalogoInvalido(
                f"El proyecto {proyecto!r} apunta a un área desconocida: {area!r}."
            )

    actualizado = crudo.get("actualizado")
    if actualizado is not None and not isinstance(actualizado, str):
        raise CatalogoInvalido("'actualizado' debe ser texto o null.")

    return CatalogoIds(
        areas=areas,
        proyectos=proyectos,
        proyecto_area={str(proyecto): str(area) for proyecto, area in mapa.items()},
        actualizado=actualizado,
    )
