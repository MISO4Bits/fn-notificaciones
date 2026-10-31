"""Envío simulado de avisos push: solo registra que el aviso se habría entregado.

Es el comportamiento por defecto (local, pruebas y cualquier entorno sin proyecto de Firebase).
No escribe el token completo ni el texto del aviso.
"""

from __future__ import annotations

import logging

from app.domain import Aviso, token_corto

logger = logging.getLogger("fn_notificaciones.adapters.push_simulado")


class LoggingPushSender:
    async def enviar(self, aviso: Aviso, token: str) -> None:
        logger.info(
            "push simulado (no se envía) aviso_id=%s destino=%s token=%s",
            aviso.id,
            aviso.destino,
            token_corto(token),
        )
