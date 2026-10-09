"""Caso de uso: recibir un mensaje de Pub/Sub y enviar el correo que trae."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind

from app.domain import EnvioTransitorio, parsear_evento
from app.ports import EmailSenderPort

logger = logging.getLogger("fn_notificaciones.envio")
tracer = trace.get_tracer("fn_notificaciones")


class NotificacionesService:
    def __init__(
        self,
        sender: EmailSenderPort,
        *,
        intentos: int = 3,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sender = sender
        self._intentos = max(1, intentos)
        self._dormir = dormir

    async def procesar(self, contenido: bytes, atributos: dict[str, str], id_mensaje: str) -> bool:
        """Envía el correo del mensaje. ``True`` si se envió, ``False`` si el mensaje
        no era para esta función.

        Lanza ``MensajeInvalido`` / ``EnvioRechazado`` (no se arreglan reintentando) o
        ``EnvioTransitorio`` cuando se agotaron los intentos (Pub/Sub debe reentregar).
        """
        correo = parsear_evento(contenido, id_mensaje)
        if correo is None:
            logger.info("mensaje ignorado: no es un EnviarCorreo")
            return False

        # El traceparent viaja en los atributos del mensaje (lo inyecta CoreTransaccional):
        # el envío queda en el mismo trace que el registro que lo originó.
        contexto = propagate.extract(atributos)
        with tracer.start_as_current_span(
            "notificaciones.enviar_correo",
            context=contexto,
            kind=SpanKind.CONSUMER,
            attributes={"correo.plantilla": correo.plantilla},
        ):
            for intento in range(1, self._intentos + 1):
                try:
                    await self._sender.enviar(correo)
                except EnvioTransitorio as exc:
                    logger.warning(
                        "envío fallido plantilla=%s intento=%s/%s motivo=%s",
                        correo.plantilla,
                        intento,
                        self._intentos,
                        exc,
                    )
                    if intento == self._intentos:
                        raise
                    await self._dormir(0.2 * 2 ** (intento - 1))
                else:
                    logger.info("correo enviado plantilla=%s", correo.plantilla)
                    return True
        return True  # pragma: no cover - el bucle siempre retorna o lanza
