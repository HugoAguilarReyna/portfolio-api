"""Geometría de las 16 alcaldías de la Ciudad de México.

Fuente: catálogo de datos abiertos de la CDMX, conjunto «Límite de Alcaldías
(áreas geoestadísticas municipales)» — CC-BY-4.0-ESP, basado en el Marco
Geoestadístico 2020 del INEGI. El archivo original se guarda tal cual en
``backend/data/atlas/alcaldias/alcaldias-cdmx.geojson`` y se simplifica en
memoria para enviarlo a la interfaz.

Se usa para tres cosas concretas:

* comprobar la **pertenencia espacial** de cada coordenada (punto dentro de la
  CDMX y en qué alcaldía);
* detectar desajustes entre el texto ``alcaldia`` del archivo y la geometría;
* dibujar el coroplético del mapa.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from app.config import BACKEND_DIR

#: Copia versionada dentro del paquete: viaja en la imagen (`COPY app ./app`).
RUTA_VERSIONADA = Path(__file__).resolve().parent / "data" / "alcaldias-cdmx.geojson"

#: Ruta del monorepo (desarrollo local): `backend/data/atlas/alcaldias/...`.
RUTA_MONOREPO = BACKEND_DIR / "data" / "atlas" / "alcaldias" / "alcaldias-cdmx.geojson"

#: GeoJSON de las alcaldías de la CDMX. La imagen Docker solo trae la copia
#: versionada; en el monorepo se usa la copia original de `backend/data`.
RUTA_GEOJSON = RUTA_VERSIONADA if RUTA_VERSIONADA.exists() else RUTA_MONOREPO


#: Atribución que la interfaz debe mostrar junto al mapa.
ATRIBUCION = {
    "conjunto": "Límite de Alcaldías (áreas geoestadísticas municipales)",
    "portal": "https://datos.cdmx.gob.mx/dataset/alcaldias",
    "licencia": "CC-BY-4.0-ESP",
    "version": "Marco Geoestadístico 2020 (INEGI), publicado por la CDMX",
    "catalogo_modificado": "2026-09-01",
    "recurso_modificado": "2026-08-28",
}

#: Caja envolvente de la Ciudad de México (para «Restablecer extensión»).
EXTENSION_CDMX = {"lon_min": -99.365, "lat_min": 19.048, "lon_max": -98.940, "lat_max": 19.593}


def _punto_en_anillo(lon: float, lat: float, anillo: list[list[float]]) -> bool:
    """Algoritmo del rayo: verdadero si el punto cae dentro del anillo."""
    dentro = False
    total = len(anillo)
    j = total - 1
    for i in range(total):
        xi, yi = anillo[i][0], anillo[i][1]
        xj, yj = anillo[j][0], anillo[j][1]
        if (yi > lat) != (yj > lat):
            cruce = (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi
            if lon < cruce:
                dentro = not dentro
        j = i
    return dentro


def _area_anillo(anillo: list[list[float]]) -> float:
    area = 0.0
    for i in range(len(anillo) - 1):
        area += anillo[i][0] * anillo[i + 1][1] - anillo[i + 1][0] * anillo[i][1]
    return abs(area) / 2


def _distancia_punto_segmento(p: list[float], a: list[float], b: list[float]) -> float:
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _simplificar(anillo: list[list[float]], tolerancia: float) -> list[list[float]]:
    """Simplificación de Douglas-Peucker iterativa sobre un anillo cerrado.

    Se resuelve con una pila explícita en vez de recursión: los anillos del
    Marco Geoestadístico tienen miles de vértices y la versión recursiva agotaba
    el límite de recursión de Python.
    """
    if len(anillo) < 3:
        return anillo
    # Se conserva el anillo cerrado: primer y último punto iguales.
    if anillo[0] != anillo[-1]:
        anillo = anillo + [anillo[0]]
    conservar = [False] * len(anillo)
    conservar[0] = conservar[-1] = True
    pila = [(0, len(anillo) - 1)]
    while pila:
        inicio, fin = pila.pop()
        if fin <= inicio + 1:
            continue
        primero, ultimo = anillo[inicio], anillo[fin]
        indice, maxima = inicio, 0.0
        for i in range(inicio + 1, fin):
            distancia = _distancia_punto_segmento(anillo[i], primero, ultimo)
            if distancia > maxima:
                indice, maxima = i, distancia
        if maxima > tolerancia:
            conservar[indice] = True
            pila.append((inicio, indice))
            pila.append((indice, fin))
    return [punto for punto, mantener in zip(anillo, conservar) if mantener]


class GeometriaAlcaldias:
    """Carga la geometría oficial y resuelve consultas espaciales."""

    def __init__(self, ruta: Path | None = None, tolerancia: float = 0.0006) -> None:
        self.ruta = ruta or RUTA_GEOJSON
        self.tolerancia = tolerancia
        self.alcaldias: list[dict[str, Any]] = []
        self._extension = dict(EXTENSION_CDMX)
        self._geojson: dict[str, Any] | None = None
        self._cargar()

    def _cargar(self) -> None:
        if not self.ruta.exists():
            self.alcaldias = []
            return
        datos = json.loads(self.ruta.read_text(encoding="utf-8"))
        for feature in datos.get("features", []):
            propiedades = feature.get("properties", {})
            nombre = propiedades.get("NOMGEO") or propiedades.get("nombre") or ""
            geometria = feature.get("geometry", {})
            anillos: list[list[list[float]]] = []
            if geometria.get("type") == "Polygon":
                anillos = [geometria["coordinates"][0]]
            elif geometria.get("type") == "MultiPolygon":
                anillos = [poligono[0] for poligono in geometria["coordinates"]]
            if not nombre or not anillos:
                continue
            principal = max(anillos, key=_area_anillo)
            self.alcaldias.append(
                {
                    "cvegeo": propiedades.get("CVEGEO"),
                    "nombre": nombre,
                    "anillos": anillos,
                    "principal": principal,
                    "centro": _centro(principal),
                }
            )
        if self.alcaldias:
            self._extension = _extension_de(self.alcaldias)

    def disponible(self) -> bool:
        return bool(self.alcaldias)

    def alcaldia_de(self, lon: float, lat: float) -> str | None:
        """Nombre de la alcaldía que contiene el punto, si está en la CDMX."""
        for alcaldia in self.alcaldias:
            for anillo in alcaldia["anillos"]:
                if _punto_en_anillo(lon, lat, anillo):
                    return alcaldia["nombre"]
        return None

    def geojson(self, simplificar: bool = True) -> dict[str, Any]:
        """FeatureCollection listo para la interfaz (anillos opcionalmente reducidos).

        El resultado se calcula una sola vez por proceso: la simplificación de los
        16 polígonos tarda lo suficiente como para no repetirla en cada petición.
        """
        if simplificar and self._geojson is not None:
            return self._geojson
        features = []
        for alcaldia in self.alcaldias:
            anillos = []
            for anillo in alcaldia["anillos"]:
                reducido = _simplificar(anillo, self.tolerancia) if simplificar else anillo
                anillos.append([[round(lon, 5), round(lat, 5)] for lon, lat in reducido])
            features.append(
                {
                    "type": "Feature",
                    "properties": {"nombre": alcaldia["nombre"], "cvegeo": alcaldia["cvegeo"]},
                    "geometry": {"type": "Polygon", "coordinates": [anillos[0]]}
                    if len(anillos) == 1
                    else {"type": "MultiPolygon", "coordinates": [[anillo] for anillo in anillos]},
                }
            )
        resultado = {
            "type": "FeatureCollection",
            "atribucion": ATRIBUCION,
            "extension": dict(self._extension),
            "features": features,
        }
        if simplificar:
            self._geojson = resultado
        return resultado

    def extension(self) -> dict[str, float]:
        return dict(self._extension)


def _extension_de(alcaldias: list[dict[str, Any]]) -> dict[str, float]:
    """Caja envolvente real de los polígonos cargados."""
    lons = [punto[0] for alcaldia in alcaldias for anillo in alcaldia["anillos"] for punto in anillo]
    lats = [punto[1] for alcaldia in alcaldias for anillo in alcaldia["anillos"] for punto in anillo]
    if not lons or not lats:
        return dict(EXTENSION_CDMX)
    return {
        "lon_min": round(min(lons), 5),
        "lat_min": round(min(lats), 5),
        "lon_max": round(max(lons), 5),
        "lat_max": round(max(lats), 5),
    }


def _centro(anillo: list[list[float]]) -> dict[str, float]:
    """Centro aproximado de la alcaldía (media de los vértices del anillo principal)."""
    total = len(anillo)
    if not total:
        return {"lon": 0.0, "lat": 0.0}
    lon = sum(punto[0] for punto in anillo) / total
    lat = sum(punto[1] for punto in anillo) / total
    return {"lon": round(lon, 5), "lat": round(lat, 5)}


_GEOMETRIA: GeometriaAlcaldias | None = None


def obtener_geometria() -> GeometriaAlcaldias:
    """Instancia compartida (la geometría se lee una vez por proceso)."""
    global _GEOMETRIA
    if _GEOMETRIA is None:
        _GEOMETRIA = GeometriaAlcaldias()
    return _GEOMETRIA
