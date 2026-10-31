"""Caso de uso: recibir un mensaje de Pub/Sub y enviar el correo que trae."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind

from app.domain import (
    Adjunto,
    Aviso,
    Correo,
    EnvioRechazado,
    EnvioTransitorio,
    TokenNoRegistrado,
    parsear_aviso,
    parsear_evento,
    token_corto,
)
from app.ports import EmailSenderPort, LectorAdjuntosPort, PushSenderPort

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


@dataclass(frozen=True)
class ResultadoPush:
    """Qué pasó con cada dispositivo del aviso."""

    enviados: int = 0
    tokens_muertos: int = 0
    rechazados: int = 0


class PushService:
    """Caso de uso: recibir un mensaje de Pub/Sub y entregar el aviso push que trae."""

    def __init__(
        self,
        sender: PushSenderPort,
        *,
        intentos: int = 3,
        concurrencia: int = 5,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sender = sender
        self._intentos = max(1, intentos)
        self._concurrencia = max(1, concurrencia)
        self._dormir = dormir

    async def procesar(
        self, contenido: bytes, atributos: dict[str, str], id_mensaje: str
    ) -> ResultadoPush | None:
        """Entrega el aviso a cada dispositivo. ``None`` si el mensaje no era para esta ruta.

        Un token muerto o rechazado no detiene a los demás. Lanza ``MensajeInvalido`` si el
        mensaje no se puede leer y ``EnvioTransitorio`` si algún dispositivo agotó los intentos
        (Pub/Sub reentrega; la etiqueta del aviso evita que el teléfono lo muestre dos veces).
        """
        aviso = parsear_aviso(contenido, id_mensaje)
        if aviso is None:
            logger.info("mensaje ignorado: no es un EnviarPush")
            return None
        logger.info(
            "push a entregar aviso_id=%s cliente_id=%s destino=%s dispositivos=%d traceparent=%s",
            aviso.id,
            aviso.cliente_id or "-",
            aviso.destino,
            len(aviso.tokens),
            "si" if "traceparent" in atributos else "no",
        )
        contexto = propagate.extract(atributos)
        with tracer.start_as_current_span(
            "notificaciones.enviar_push",
            context=contexto,
            kind=SpanKind.CONSUMER,
            attributes={"push.dispositivos": len(aviso.tokens), "push.destino": aviso.destino},
        ):
            cupo = asyncio.Semaphore(self._concurrencia)

            async def _con_cupo(token: str) -> str:
                async with cupo:
                    return await self._entregar(aviso, token)

            resultados = await asyncio.gather(
                *(_con_cupo(t) for t in aviso.tokens), return_exceptions=True
            )
        transitorio = next((r for r in resultados if isinstance(r, EnvioTransitorio)), None)
        if transitorio is not None:
            raise transitorio
        inesperado = next((r for r in resultados if isinstance(r, BaseException)), None)
        if inesperado is not None:
            raise inesperado
        return ResultadoPush(
            enviados=resultados.count("enviado"),
            tokens_muertos=resultados.count("token_muerto"),
            rechazados=resultados.count("rechazado"),
        )

    async def _entregar(self, aviso: Aviso, token: str) -> str:
        for intento in range(1, self._intentos + 1):
            try:
                await self._sender.enviar(aviso, token)
            except TokenNoRegistrado:
                logger.info(
                    "token muerto aviso_id=%s token=%s: la app ya no lo usa",
                    aviso.id,
                    token_corto(token),
                )
                return "token_muerto"
            except EnvioRechazado as exc:
                logger.error(
                    "aviso rechazado aviso_id=%s token=%s motivo=%s",
                    aviso.id,
                    token_corto(token),
                    exc,
                )
                return "rechazado"
            except EnvioTransitorio as exc:
                logger.warning(
                    "envío fallido aviso_id=%s token=%s intento=%s/%s motivo=%s",
                    aviso.id,
                    token_corto(token),
                    intento,
                    self._intentos,
                    exc,
                )
                if intento == self._intentos:
                    raise
                await self._dormir(0.2 * 2 ** (intento - 1))
            else:
                logger.info("push enviado aviso_id=%s token=%s", aviso.id, token_corto(token))
                return "enviado"
        raise AssertionError("el bucle siempre retorna o lanza")  # pragma: no cover
