"""Envío real de avisos push por Firebase Cloud Messaging (interfaz HTTP v1).

Se autentica con las credenciales de la cuenta de servicio de la función (Application Default
Credentials), sin llaves en archivos. Esa cuenta necesita ``roles/firebasecloudmessaging.admin``.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.domain import Aviso, EnvioRechazado, EnvioTransitorio, TokenNoRegistrado, token_corto

logger = logging.getLogger("fn_notificaciones.adapters.fcm")

URL = "https://fcm.googleapis.com/v1/projects/{proyecto}/messages:send"
ALCANCE = "https://www.googleapis.com/auth/firebase.messaging"
_PRIORIDAD = {"alta": "HIGH", "normal": "NORMAL"}
# El token de ese dispositivo no sirve más: se descarta el dispositivo, no el aviso.
_TOKEN_MUERTO = {"UNREGISTERED"}
# Rechazos definitivos del mensaje o del token: reintentar no los arregla.
_RECHAZOS = {"INVALID_ARGUMENT", "SENDER_ID_MISMATCH"}


def _codigo_fcm(respuesta: httpx.Response) -> str:
    """Código del error de Firebase (``UNREGISTERED``, ``INVALID_ARGUMENT``…). Nunca el mensaje:
    puede nombrar el token."""
    try:
        error = respuesta.json().get("error", {})
    except (ValueError, AttributeError):
        return ""
    detalles = error.get("details", []) if isinstance(error, dict) else []
    for detalle in detalles:
        codigo = detalle.get("errorCode") if isinstance(detalle, dict) else None
        if isinstance(codigo, str) and len(codigo) <= 64:
            return codigo
    estado = error.get("status") if isinstance(error, dict) else None
    return estado if isinstance(estado, str) and len(estado) <= 64 else ""


class FcmPushSender:
    def __init__(
        self,
        proyecto: str,
        *,
        ttl_s: int = 24 * 60 * 60,
        timeout_s: float = 5.0,
        client: httpx.AsyncClient | None = None,
        credenciales=None,
    ) -> None:
        self._url = URL.format(proyecto=proyecto)
        self._ttl_s = ttl_s
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._credenciales = credenciales

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _token_de_acceso(self) -> str:
        import google.auth
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import Request

        try:
            if self._credenciales is None:
                self._credenciales, _ = await asyncio.to_thread(
                    google.auth.default, scopes=[ALCANCE]
                )
            if not self._credenciales.valid:
                await asyncio.to_thread(self._credenciales.refresh, Request())
        except GoogleAuthError as exc:
            # Es configuración (cuenta de servicio sin permiso o sin metadatos): se devuelve a
            # Pub/Sub para que el aviso salga cuando se corrija.
            raise EnvioTransitorio("no se pudo obtener el token de acceso de Firebase") from exc
        return self._credenciales.token

    async def enviar(self, aviso: Aviso, token: str) -> None:
        acceso = await self._token_de_acceso()
        mensaje = {
            "token": token,
            "notification": {"title": aviso.titulo, "body": aviso.cuerpo},
            # Los valores de ``data`` son siempre texto. Al tocar el aviso, la app los recibe.
            "data": {"destino": aviso.destino, "id": aviso.destino_id},
            "android": {
                "priority": _PRIORIDAD[aviso.prioridad],
                "ttl": f"{self._ttl_s}s",
                # Una reentrega de Pub/Sub reemplaza el aviso en la bandeja en vez de duplicarlo.
                "notification": {"tag": aviso.id},
            },
        }
        inicio = time.perf_counter()
        try:
            respuesta = await self._client.post(
                self._url,
                json={"message": mensaje},
                headers={"Authorization": f"Bearer {acceso}"},
            )
        except httpx.TransportError as exc:
            logger.warning(
                "fcm: sin respuesta aviso_id=%s token=%s error=%s latencia_ms=%d",
                aviso.id,
                token_corto(token),
                type(exc).__name__,
                (time.perf_counter() - inicio) * 1000,
            )
            raise EnvioTransitorio("Firebase no está disponible") from exc

        estado = respuesta.status_code
        latencia_ms = (time.perf_counter() - inicio) * 1000
        if estado < 300:
            logger.info(
                "fcm: aceptado aviso_id=%s token=%s estado=%s latencia_ms=%d",
                aviso.id,
                token_corto(token),
                estado,
                latencia_ms,
            )
            return
        codigo = _codigo_fcm(respuesta)
        logger.warning(
            "fcm: rechazo aviso_id=%s token=%s estado=%s codigo=%s latencia_ms=%d",
            aviso.id,
            token_corto(token),
            estado,
            codigo or "-",
            latencia_ms,
        )
        detalle = f"{estado} {codigo}" if codigo else str(estado)
        if codigo in _TOKEN_MUERTO:
            raise TokenNoRegistrado(f"Firebase no conoce el token ({detalle})")
        if codigo in _RECHAZOS:
            raise EnvioRechazado(f"Firebase rechazó el aviso ({detalle})")
        # 429, 5xx, UNAVAILABLE y también 401/403 sin código propio (permiso de la cuenta de
        # servicio): se reintenta o se devuelve a Pub/Sub, no se pierde el aviso.
        raise EnvioTransitorio(f"Firebase respondió {detalle}")
