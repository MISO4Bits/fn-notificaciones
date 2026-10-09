from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from httpx import ASGITransport, AsyncClient

from app.api.app import create_app
from app.config import Settings
from app.domain import Correo, EnvioRechazado, EnvioTransitorio
from app.services import NotificacionesService

SPEC_PATH = Path(__file__).resolve().parents[1] / "openapi" / "openapi.yaml"

EVENTO_VALIDO = {
    "tipo": "EnviarCorreo",
    "id": "evento-1",
    "ocurridoEn": "2026-10-08T21:30:00Z",
    "datos": {
        "destinatario": "ana.rios@example.com",
        "plantilla": "bienvenida",
        "plantillaVersion": "V1",
        "clienteId": "cliente-1",
        "asunto": "Hola Ana",
        "cuerpoHtml": "<p>Hola Ana</p>",
        "cuerpoTexto": "Hola Ana",
    },
}


def sobre_push(evento: dict | bytes | str, *, atributos: dict | None = None) -> dict:
    """Sobre que Pub/Sub entrega a una suscripción push."""
    if isinstance(evento, dict):
        evento = json.dumps(evento)
    if isinstance(evento, str):
        evento = evento.encode()
    return {
        "message": {
            "data": base64.b64encode(evento).decode(),
            "attributes": atributos or {"tipo": "EnviarCorreo"},
            "messageId": "mensaje-1",
        },
        "subscription": "projects/p/subscriptions/s",
    }


class SenderEspia:
    """Doble del puerto de envío: guarda lo enviado y puede fallar a pedido."""

    def __init__(self, *errores: Exception) -> None:
        self.enviados: list[Correo] = []
        self._errores = list(errores)

    async def enviar(self, correo: Correo) -> None:
        if self._errores:
            error = self._errores.pop(0)
            if error is not None:
                raise error
        self.enviados.append(correo)


@pytest.fixture(scope="session")
def openapi_spec() -> dict:
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def settings() -> Settings:
    return Settings(sender_backend="logging", intentos_envio=3)


@pytest.fixture
def sender() -> SenderEspia:
    return SenderEspia()


async def _sin_espera(_segundos: float) -> None:
    return None


@pytest.fixture
def app(settings, sender):
    application = create_app(settings)
    application.state.service = NotificacionesService(sender, intentos=3, dormir=_sin_espera)
    return application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


__all__ = ["EVENTO_VALIDO", "EnvioRechazado", "EnvioTransitorio", "SenderEspia", "sobre_push"]
