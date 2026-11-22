"""API local de «Briefing con evidencia · Banco de México».

La API es de solo lectura para el corpus y de consulta para la recuperación. El
navegador nunca recibe la URI de MongoDB ni claves de proveedor: solo el estado
booleano de Atlas/modelo y las evidencias recuperadas.

Se usa ``GET /query?question=...`` para que el navegador pueda consultar desde el
sitio estático con el CORS de solo lectura ya configurado en el proyecto.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import DatabaseDep, SettingsDep
from app.rag_banxico.service import BriefingBanxico, CorpusError

router = APIRouter(prefix="/briefing-banxico", tags=["Briefing con evidencia · Banxico"])


def get_briefing_service(db: DatabaseDep, settings: SettingsDep) -> BriefingBanxico:
    try:
        return BriefingBanxico(db, settings)
    except CorpusError as exc:
        # El corpus es obligatorio; Atlas y el modelo son opcionales.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


BriefingDep = Annotated[BriefingBanxico, Depends(get_briefing_service)]


@router.get("/health", summary="Estado del corpus, Atlas y modelo")
def health(servicio: BriefingDep) -> dict:
    """Devuelve booleanos y nombres de colección; nunca URI ni secretos."""
    return servicio.salud()


@router.get("/corpus", summary="Corpus documental versionable")
def corpus(servicio: BriefingDep) -> dict:
    return servicio.corpus


@router.get("/query", summary="Recupera evidencia y responde solo con ella")
def query(
    servicio: BriefingDep,
    question: Annotated[
        str,
        Query(
            min_length=3,
            max_length=1200,
            description="Pregunta sobre los documentos cargados.",
        ),
    ],
) -> dict:
    pregunta = question.strip()
    if len(pregunta) < 3:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="La pregunta debe tener al menos 3 caracteres.",
        )
    return servicio.consultar(pregunta)
