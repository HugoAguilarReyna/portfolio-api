"""Router de la versión 1 de la API."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    atlas,
    briefing_banxico,
    cnbv,
    comercial,
    copiloto_nl_sql,
    datasets,
    dockmind,
    health,
    incident_commander,
    kilometro_cero,
    marketing,
    profitability,
    ripple_room,
    walmex,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(datasets.router)
api_router.include_router(profitability.router)
api_router.include_router(cnbv.router)
api_router.include_router(atlas.router)
api_router.include_router(marketing.router)
api_router.include_router(walmex.router)
api_router.include_router(comercial.router)
api_router.include_router(briefing_banxico.router)
api_router.include_router(copiloto_nl_sql.router)
api_router.include_router(incident_commander.router)
api_router.include_router(kilometro_cero.router)
api_router.include_router(ripple_room.router)
api_router.include_router(dockmind.router)
