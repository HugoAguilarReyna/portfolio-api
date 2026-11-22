"""Destino de MongoDB del proyecto comercial, verificado antes de escribir.

Motivo
------
En un incremento anterior un script de este proyecto escribió por error en la base
**remota** del portafolio (Atlas) porque heredaba la URI principal. Este módulo
existe para que eso no pueda repetirse: el proyecto tiene su propia variable
(``COMERCIAL_MONGODB_URI``) y su propio destino, y aquí se comprueba **antes de
abrir cualquier conexión**.

Reglas (se aplican sin tocar la red)
------------------------------------
- Solo ``mongodb://``; ``mongodb+srv://`` se rechaza siempre.
- Hosts locales y el servicio Docker interno ``mongo-m5`` (base y puerto fijos).
- Sin cadenas de conexión remotas ni hosts distintos del MongoDB local autorizado.
- La base es exclusiva del proyecto (``atlas_comercial_local`` por omisión).
- Si la URI y la configuración declaran bases distintas, se rechaza la
  contradicción en vez de elegir una en silencio.
- Nunca se muestran credenciales: ``describir()`` devuelve solo host, puerto y base.
"""

from __future__ import annotations

from urllib.parse import urlsplit

BASE_POR_OMISION = "atlas_comercial_local"
"""Base exclusiva del proyecto en MongoDB local."""

HOSTS_LOCALES = frozenset({"127.0.0.1", "localhost", "::1"})
HOST_DOCKER_M5 = "mongo-m5"

ESQUEMAS_PERMITIDOS = ("mongodb://",)


class DestinoNoPermitido(RuntimeError):
    """La URI configurada no es un MongoDB local autorizado para este proyecto."""


def resolver_destino(uri: str | None, base_declarada: str | None = None) -> tuple[str, str]:
    """Valida la URI y devuelve ``(uri, base)`` sin abrir conexión.

    Lanza :class:`DestinoNoPermitido` con un mensaje que **no** contiene la cadena
    de conexión ni credenciales.
    """
    texto = (uri or "").strip()
    if not texto:
        raise DestinoNoPermitido(
            "Falta COMERCIAL_MONGODB_URI: el proyecto comercial no hereda la URI principal "
            f"del portafolio. Configura mongodb://127.0.0.1:27017/{BASE_POR_OMISION}"
        )
    if texto.startswith("mongodb+srv://"):
        raise DestinoNoPermitido(
            "mongodb+srv:// no está permitido: el Atlas comercial trabaja solo contra "
            "MongoDB local hasta la migración explícita."
        )
    if not texto.startswith(ESQUEMAS_PERMITIDOS):
        raise DestinoNoPermitido("COMERCIAL_MONGODB_URI debe empezar por mongodb://")

    partes = urlsplit(texto)
    host = (partes.hostname or "").strip().lower()
    if host not in HOSTS_LOCALES and host != HOST_DOCKER_M5:
        raise DestinoNoPermitido(
            "Destino no autorizado: solo se admite MongoDB local "
            f"({', '.join(sorted(HOSTS_LOCALES))}). Se rechaza cualquier host remoto."
        )
    if partes.port is not None and not (1 <= partes.port <= 65_535):
        raise DestinoNoPermitido("Puerto de MongoDB fuera de rango")

    base_uri = partes.path.lstrip("/").split("?")[0].strip()
    base_config = (base_declarada or "").strip()
    if base_uri and base_config and base_uri != base_config:
        raise DestinoNoPermitido(
            "Contradicción de destino: la URI y COMERCIAL_MONGODB_DB declaran bases distintas "
            f"({base_uri!r} frente a {base_config!r})."
        )
    base = base_config or base_uri or BASE_POR_OMISION
    if host == HOST_DOCKER_M5 and (
        partes.port not in (None, 27017) or base != BASE_POR_OMISION
    ):
        raise DestinoNoPermitido(
            "El servicio Docker mongo-m5 solo admite el puerto 27017 y la base "
            "atlas_comercial_local."
        )
    if not base.replace("_", "").isalnum():
        raise DestinoNoPermitido(
            "Nombre de base no válido: se admiten letras, números y guion bajo."
        )
    return texto, base


def describir(uri: str) -> str:
    """``host:puerto/base`` sin usuario, contraseña ni parámetros."""
    partes = urlsplit(uri)
    host = partes.hostname or "?"
    puerto = f":{partes.port}" if partes.port else ""
    base = partes.path.lstrip("/").split("?")[0] or BASE_POR_OMISION
    return f"{host}{puerto}/{base}"


def es_local(uri: str) -> bool:
    """True si el destino es un MongoDB local permitido (sin abrir conexión)."""
    try:
        resolver_destino(uri)
    except DestinoNoPermitido:
        return False
    return True
