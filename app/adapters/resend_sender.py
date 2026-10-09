"""Envío real por la API HTTPS de Resend."""

from __future__ import annotations

import logging

import httpx

from app.domain import Correo, EnvioRechazado, EnvioTransitorio

logger = logging.getLogger("fn_notificaciones.adapters.resend")

URL = "https://api.resend.com/emails"
# Límite de tasa y errores del proveedor: pueden resolverse solos.
_REINTENTABLES = {429}
# Credenciales: es un problema de configuración, no del mensaje — se devuelve a
# Pub/Sub para que se entregue de nuevo cuando se corrija, en vez de perderlo.
_CONFIGURACION = {401, 403}


class ResendEmailSender:
    def __init__(
        self,
        api_key: str,
        remitente: str,
        reply_to: str | None = None,
        timeout_s: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._remitente = remitente
        self._reply_to = reply_to
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def enviar(self, correo: Correo) -> None:
        cuerpo = {
            "from": self._remitente,
            "to": [correo.destinatario],
            "subject": correo.asunto,
            "html": correo.cuerpo_html,
            "text": correo.cuerpo_texto,
        }
        if self._reply_to:
            cuerpo["reply_to"] = self._reply_to
        try:
            respuesta = await self._client.post(
                URL,
                json=cuerpo,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    # Una reentrega del mismo evento no manda el correo dos veces.
                    "Idempotency-Key": correo.id,
                },
            )
        except httpx.TransportError as exc:
            raise EnvioTransitorio("Resend no está disponible") from exc

        estado = respuesta.status_code
        if estado < 300:
            return
        # El cuerpo del error puede nombrar el destinatario: no se registra.
        if estado >= 500 or estado in _REINTENTABLES or estado in _CONFIGURACION:
            raise EnvioTransitorio(f"Resend respondió {estado}")
        raise EnvioRechazado(f"Resend rechazó el correo ({estado})")
