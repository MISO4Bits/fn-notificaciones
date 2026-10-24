"""Fábrica simple de adaptadores a partir de la configuración (sin framework de DI)."""

from __future__ import annotations

from app.adapters.logging_sender import LoggingEmailSender
from app.adapters.resend_sender import ResendEmailSender
from app.config import Settings
from app.ports import EmailSenderPort, LectorAdjuntosPort


def build_sender(settings: Settings) -> EmailSenderPort:
    if settings.sender_backend == "logging":
        return LoggingEmailSender()
    if settings.sender_backend == "resend":
        if not settings.resend_api_key:
            raise ValueError("FN_RESEND_API_KEY es obligatoria con FN_SENDER_BACKEND=resend")
        return ResendEmailSender(
            settings.resend_api_key,
            settings.remitente,
            settings.reply_to,
            settings.resend_timeout_s,
        )
    raise ValueError(f"sender_backend no soportado: {settings.sender_backend}")


def build_lector(settings: Settings) -> LectorAdjuntosPort | None:
    """Sin bucket de adjuntos no hay lector: un correo que pida adjuntos se reentrega."""
    if not settings.adjuntos_bucket:
        return None
    from app.adapters.gcs_lector import LectorGcs  # pragma: no cover - requiere GCP real

    return LectorGcs()  # pragma: no cover
