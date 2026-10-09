from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración por variables de entorno (prefijo ``FN_``)."""

    model_config = SettingsConfigDict(env_prefix="FN_", env_file=".env", extra="ignore")

    service_name: str = "fn-notificaciones"
    environment: str = "local"

    # Quién envía: "logging" (simulado: solo registra, no envía nada) | "resend"
    sender_backend: str = "logging"
    # Clave de API de Resend. Sale de Secret Manager; nunca va en el repo.
    resend_api_key: str | None = None
    remitente: str = "Solventa <no-reply@notificaciones.solventa4bits.com>"
    # Dirección a la que responde quien contesta el correo (alias de reenvío del dominio).
    reply_to: str | None = None
    resend_timeout_s: float = 5.0

    # Intentos de envío por mensaje antes de devolverlo a Pub/Sub (BITS-119 AC-6).
    intentos_envio: int = 3

    # Observabilidad (DI-008): la función corre fuera del cluster, así que exporta
    # directo a Grafana Cloud por OTLP/HTTP. El destino y las credenciales salen de
    # las variables estándar OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_HEADERS.
    otel_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
