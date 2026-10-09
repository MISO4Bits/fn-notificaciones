from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response

from app.adapters.factory import build_sender
from app.api.routes import router
from app.config import Settings, get_settings
from app.logging_utils import SinRuidoDeHealthCheck
from app.services import NotificacionesService
from app.telemetry import setup_telemetry, shutdown_telemetry

SPEC_PATH = Path(__file__).resolve().parents[2] / "openapi" / "openapi.yaml"
logger = logging.getLogger("fn_notificaciones.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("uvicorn.access").addFilter(SinRuidoDeHealthCheck())

    telemetry = setup_telemetry(settings)
    sender = build_sender(settings)
    service = NotificacionesService(sender, intentos=settings.intentos_envio)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        aclose = getattr(sender, "aclose", None)
        if aclose is not None:
            await aclose()
        shutdown_telemetry(telemetry)

    app = FastAPI(title="fn-notificaciones", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.service = service
    app.state.telemetry = telemetry

    @app.exception_handler(RequestValidationError)
    async def _sobre_invalido(_request, _exc: RequestValidationError) -> Response:
        # Un sobre que no es de Pub/Sub no se arregla reintentando: se confirma y se anota.
        logger.error("solicitud descartada: no es un sobre de Pub/Sub")
        return Response(status_code=204)

    app.include_router(router)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        return {"status": "ok", "service": settings.service_name}

    if SPEC_PATH.exists():

        @app.get("/openapi.yaml", include_in_schema=False)
        async def openapi_yaml() -> FileResponse:
            return FileResponse(SPEC_PATH, media_type="application/yaml")

    return app
