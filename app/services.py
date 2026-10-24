"""Caso de uso: recibir un mensaje de Pub/Sub y enviar el correo que trae."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind

from app.domain import (
    Adjunto,
    Correo,
    EnvioRechazado,
    EnvioTransitorio,
    parsear_evento,
)
from app.ports import EmailSenderPort, LectorAdjuntosPort

logger = logging.getLogger("fn_notificaciones.envio")
tracer = trace.get_tracer("fn_notificaciones")


class NotificacionesService:
    def __init__(
        self,
        sender: EmailSenderPort,
        *,
        intentos: int = 3,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
        lector: LectorAdjuntosPort | None = None,
        bucket_permitido: str | None = None,
        max_adjuntos_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self._lector = lector
        self._bucket_permitido = bucket_permitido
        self._max_adjuntos_bytes = max_adjuntos_bytes
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
        # cliente_id es un UUID opaco; del destinatario solo el dominio.
        logger.info(
            "correo a enviar plantilla=%s correo_id=%s cliente_id=%s destino_dominio=%s "
            "traceparent=%s",
            correo.plantilla,
            correo.id,
            correo.cliente_id or "-",
            correo.destinatario.rsplit("@", 1)[-1],
            "si" if "traceparent" in atributos else "no",
        )

        # El traceparent viaja en los atributos del mensaje (lo inyecta CoreTransaccional):
        # el envío queda en el mismo trace que el registro que lo originó.
        contexto = propagate.extract(atributos)
        with tracer.start_as_current_span(
            "notificaciones.enviar_correo",
            context=contexto,
            kind=SpanKind.CONSUMER,
            attributes={"correo.plantilla": correo.plantilla},
        ):
            correo = await self._con_adjuntos(correo)
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

    async def _con_adjuntos(self, correo: Correo) -> Correo:
        """Lee del bucket los archivos que el evento pide adjuntar."""
        if not correo.referencias_adjuntos:
            return correo
        if self._lector is None or self._bucket_permitido is None:
            # Falta configuración (bucket y permiso de lectura): se reentrega en vez de perder
            # el correo, para que salga cuando se corrija.
            raise EnvioTransitorio("la función no tiene configurado el bucket de adjuntos")
        for referencia in correo.referencias_adjuntos:
            if referencia.bucket != self._bucket_permitido:
                # Un evento no puede hacer que la función lea archivos de cualquier bucket.
                raise EnvioRechazado("el adjunto es de un bucket que no está permitido")
        adjuntos: list[Adjunto] = []
        restante = self._max_adjuntos_bytes
        for referencia in correo.referencias_adjuntos:
            adjunto = await self._lector.leer(referencia, restante)
            restante -= len(adjunto.contenido)
            adjuntos.append(adjunto)
            logger.info(
                "adjunto leído correo_id=%s nombre=%s bytes=%d",
                correo.id,
                adjunto.nombre,
                len(adjunto.contenido),
            )
        return replace(correo, adjuntos=tuple(adjuntos))
