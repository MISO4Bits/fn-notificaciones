from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.schemas import SobrePush
from app.domain import EnvioRechazado, EnvioTransitorio, MensajeInvalido, decodificar
from app.services import NotificacionesService
from app.telemetry import vaciar

logger = logging.getLogger("fn_notificaciones.api")
router = APIRouter()


def get_service(request: Request) -> NotificacionesService:
    return request.app.state.service


ServiceDep = Annotated[NotificacionesService, Depends(get_service)]


@router.post(
    "/",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Pub/Sub"],
    responses={503: {"description": "El envío falló; Pub/Sub debe reentregar el mensaje."}},
)
async def recibir_mensaje(sobre: SobrePush, request: Request, service: ServiceDep) -> Response:
    """Endpoint de la suscripción *push*: cualquier 2xx confirma el mensaje, cualquier
    otro código hace que Pub/Sub lo reentregue con espera creciente.

    Un mensaje que nunca podrá enviarse (mal formado, rechazado por el proveedor) se
    confirma igual, registrando el error: devolverlo solo lo haría circular sin fin.
    """
    try:
        contenido = decodificar(sobre.message.data)
        await service.procesar(contenido, sobre.message.attributes, sobre.message.message_id)
    except (MensajeInvalido, EnvioRechazado) as exc:
        logger.error("mensaje descartado id=%s motivo=%s", sobre.message.message_id, exc)
    except EnvioTransitorio:
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    finally:
        vaciar(request.app.state.telemetry)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
