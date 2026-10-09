"""Envío simulado: solo registra que el correo se habría enviado.

Es el comportamiento por defecto (local, pruebas y cualquier entorno sin la clave del
proveedor). No escribe el destinatario ni el contenido: traen datos personales.
"""

from __future__ import annotations

import logging

from app.domain import Correo

logger = logging.getLogger("fn_notificaciones.adapters.simulado")


class LoggingEmailSender:
    async def enviar(self, correo: Correo) -> None:
        logger.info("correo simulado (no se envía) plantilla=%s id=%s", correo.plantilla, correo.id)
