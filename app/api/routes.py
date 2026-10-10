from __future__ import annotations

import logging
import time
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
    mensaje = sobre.message
    # Solo nombres de atributos y el tipo de evento: ningún dato del cliente.
    logger.info(
        "mensaje recibido id=%s intento_entrega=%s publicado=%s tipo=%s "
        "atributos=%s bytes_base64=%s",
        mensaje.message_id,
        sobre.delivery_attempt if sobre.delivery_attempt is not None else "-",
        mensaje.publish_time or "-",
        mensaje.attributes.get("tipo", "-"),
        sorted(mensaje.attributes),
        len(mensaje.data),
    )
    inicio = time.perf_counter()
    resultado = "error"
    try:
        contenido = decodificar(mensaje.data)
        enviado = await service.procesar(contenido, mensaje.attributes, mensaje.message_id)
        resultado = "enviado" if enviado else "ignorado"
    except (MensajeInvalido, EnvioRechazado) as exc:
        resultado = "descartado"
        logger.error("mensaje descartado id=%s motivo=%s", mensaje.message_id, exc)
    except EnvioTransitorio as exc:
        resultado = "reintentar"
        logger.warning("mensaje se devuelve a Pub/Sub id=%s motivo=%s", mensaje.message_id, exc)
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    finally:
        logger.info(
            "mensaje procesado id=%s resultado=%s duracion_ms=%d",
            mensaje.message_id,
            resultado,
            (time.perf_counter() - inicio) * 1000,
        )
        vaciar(request.app.state.telemetry)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
