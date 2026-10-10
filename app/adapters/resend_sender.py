"""Envío real por la API HTTPS de Resend."""

from __future__ import annotations

import logging
import re
import time

import httpx

from app.domain import Correo, EnvioRechazado, EnvioTransitorio

logger = logging.getLogger("fn_notificaciones.adapters.resend")

URL = "https://api.resend.com/emails"
# Límite de tasa y errores del proveedor: pueden resolverse solos.
_REINTENTABLES = {429}
# Credenciales: es un problema de configuración, no del mensaje — se devuelve a
# Pub/Sub para que se entregue de nuevo cuando se corrija, en vez de perderlo.
_CONFIGURACION = {401, 403}

# Separadores entre "palabras". El texto se parte en tokens y se enmascara el que lleva
# `@` (en vez de una expresión que busque `algo@algo`, que es cuadrática sin ancla).
_SEPARADORES = re.compile(r"([\s<>\"',;]+)")
_CODIGO_ERROR = re.compile(r"^[a-z0-9_]{1,64}$")
_MAX_MENSAJE = 300


def _dominio(direccion: str) -> str:
    """Solo el dominio de una dirección (`Nombre <a@b.com>` o `a@b.com`): no es dato personal."""
    return direccion.rsplit("@", 1)[-1].strip(" >") if "@" in direccion else "?"


def _enmascarar_direcciones(texto: str) -> str:
    """Reemplaza por ``***`` todo token que lleve ``@`` (direcciones de correo).

    Conservador a propósito: un token como ``@@@`` también se enmascara. Los
    separadores (espacios, comas, comillas…) se conservan tal cual.
    """
    return "".join("***" if "@" in parte else parte for parte in _SEPARADORES.split(texto))


def _detalle_error(respuesta: httpx.Response) -> tuple[str, str]:
    """(código, mensaje) del error de Resend, listos para el log.

    El código (``validation_error``, ``invalid_from_address``…) no lleva datos del
    cliente. El mensaje sí podría nombrar al destinatario: se enmascaran las
    direcciones de correo y se recorta. Si el cuerpo no es el JSON esperado, se
    devuelven valores vacíos en vez de registrar texto desconocido.
    """
    try:
        cuerpo = respuesta.json()
    except ValueError:
        return "", ""
    if not isinstance(cuerpo, dict):
        return "", ""
    codigo = cuerpo.get("name")
    codigo = codigo if isinstance(codigo, str) and _CODIGO_ERROR.match(codigo) else ""
    mensaje = cuerpo.get("message")
    mensaje = _enmascarar_direcciones(mensaje)[:_MAX_MENSAJE] if isinstance(mensaje, str) else ""
    return codigo, mensaje


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
        # Qué se manda, sin datos personales: dominios, tamaños e identificadores.
        logger.info(
            "resend: enviando plantilla=%s correo_id=%s remitente_dominio=%s "
            "destino_dominio=%s reply_to=%s asunto_chars=%s html_bytes=%s texto_bytes=%s",
            correo.plantilla,
            correo.id,
            _dominio(self._remitente),
            _dominio(correo.destinatario),
            "si" if self._reply_to else "no",
            len(correo.asunto),
            len(correo.cuerpo_html.encode()),
            len(correo.cuerpo_texto.encode()),
        )
        inicio = time.perf_counter()
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
            logger.warning(
                "resend: sin respuesta plantilla=%s correo_id=%s error=%s latencia_ms=%d",
                correo.plantilla,
                correo.id,
                type(exc).__name__,
                (time.perf_counter() - inicio) * 1000,
            )
            raise EnvioTransitorio("Resend no está disponible") from exc

        estado = respuesta.status_code
        latencia_ms = (time.perf_counter() - inicio) * 1000
        if estado < 300:
            resend_id = _id_resend(respuesta)
            logger.info(
                "resend: aceptado plantilla=%s correo_id=%s estado=%s resend_id=%s latencia_ms=%d",
                correo.plantilla,
                correo.id,
                estado,
                resend_id,
                latencia_ms,
            )
            return
        codigo, mensaje = _detalle_error(respuesta)
        logger.warning(
            "resend: rechazo plantilla=%s correo_id=%s estado=%s codigo=%s mensaje=%s "
            "latencia_ms=%d",
            correo.plantilla,
            correo.id,
            estado,
            codigo or "-",
            mensaje or "-",
            latencia_ms,
        )
        # El motivo que sube por la excepción lleva solo el código, nunca el mensaje.
        detalle = f"{estado} {codigo}" if codigo else str(estado)
        if estado >= 500 or estado in _REINTENTABLES or estado in _CONFIGURACION:
            raise EnvioTransitorio(f"Resend respondió {detalle}")
        raise EnvioRechazado(f"Resend rechazó el correo ({detalle})")


def _id_resend(respuesta: httpx.Response) -> str:
    try:
        cuerpo = respuesta.json()
    except ValueError:
        return "-"
    valor = cuerpo.get("id") if isinstance(cuerpo, dict) else None
    return valor if isinstance(valor, str) and len(valor) <= 64 else "-"
