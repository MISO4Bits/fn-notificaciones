"""Puertos del hexágono. Los adaptadores viven en ``app/adapters``."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain import Adjunto, Correo, ReferenciaAdjunto


@runtime_checkable
class EmailSenderPort(Protocol):
    async def enviar(self, correo: Correo) -> None:
        """Entrega el correo al proveedor.

        Lanza ``EnvioTransitorio`` si conviene reintentar y ``EnvioRechazado`` si el
        proveedor no lo aceptará nunca.
        """
        ...


@runtime_checkable
class LectorAdjuntosPort(Protocol):
    async def leer(self, referencia: ReferenciaAdjunto, max_bytes: int) -> Adjunto:
        """Lee el archivo del bucket.

        Lanza ``EnvioTransitorio`` si no se puede leer ahora (el bucket no responde o el
        archivo todavía no aparece) y ``EnvioRechazado`` si supera ``max_bytes``.
        """
        ...
