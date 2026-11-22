"""API local del «Copiloto NL-to-SQL · indicadores económicos de México».

La API recibe una pregunta, la reduce a un plan cerrado y ejecuta una plantilla
SQL parametrizada sobre SQLite de solo lectura. El navegador no recibe rutas
internas, credenciales ni la URI de MongoDB.

Se usa ``GET /query?question=...`` para conservar el CORS de solo lectura ya
configurado en el portafolio, igual que el briefing documental.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import DatabaseDep, SettingsDep
from app.nl_sql.service import CopilotoNlSql, DatasetError, QuestionError

router = APIRouter(
    prefix="/copiloto-nl-sql",
    tags=["Copiloto NL-to-SQL · indicadores de México"],
)


def get_copiloto_service(db: DatabaseDep, settings: SettingsDep) -> CopilotoNlSql:
    try:
        return CopilotoNlSql(db, settings)
    except DatasetError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


CopilotoDep = Annotated[CopilotoNlSql, Depends(get_copiloto_service)]


@router.get("/health", summary="Estado del snapshot, SQLite, Atlas y modelo")
def health(servicio: CopilotoDep) -> dict:
    """Expone booleanos y nombres de colección; nunca secretos."""
    return servicio.salud()


@router.get("/data", summary="Snapshot WDI versionable")
def data(servicio: CopilotoDep) -> dict:
    return servicio.datos()


@router.get("/query", summary="Interpreta una pregunta y ejecuta SQL de plantilla")
def query(
    servicio: CopilotoDep,
    question: Annotated[
        str,
        Query(
            min_length=5,
            max_length=500,
            description="Pregunta sobre PIB, inflación o desempleo de México.",
        ),
    ],
) -> dict:
    pregunta = question.strip()
    if len(pregunta) < 5:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="La pregunta debe tener al menos 5 caracteres.",
        )
    try:
        return servicio.consultar(pregunta)
    except QuestionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
