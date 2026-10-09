"""Puertos del hexágono. Los adaptadores viven en ``app/adapters``."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain import Correo


@runtime_checkable
class EmailSenderPort(Protocol):
    async def enviar(self, correo: Correo) -> None:
        """Entrega el correo al proveedor.

        Lanza ``EnvioTransitorio`` si conviene reintentar y ``EnvioRechazado`` si el
        proveedor no lo aceptará nunca.
        """
        ...
