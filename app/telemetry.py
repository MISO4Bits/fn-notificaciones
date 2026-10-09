"""Instrumentación OpenTelemetry (DI-008): trazas y logs por OTLP/HTTP directo a
Grafana Cloud.

A diferencia de los servicios del cluster (que exportan al receptor de Grafana
Alloy), esta función corre en Cloud Run y no alcanza ese receptor. El endpoint y
las credenciales salen de las variables estándar ``OTEL_EXPORTER_OTLP_ENDPOINT`` y
``OTEL_EXPORTER_OTLP_HEADERS`` (esta última, de Secret Manager).

No se instrumenta FastAPI: Pub/Sub no manda ``traceparent`` como encabezado HTTP,
va en los atributos del mensaje. El span del envío lo crea ``NotificacionesService``
con ese contexto.
"""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import Settings

Telemetry = tuple[TracerProvider, LoggerProvider]

_FLUSH_MS = 2000


class _AtributosDeTraza(logging.Filter):
    """Agrega ``trace_id``/``span_id`` al texto del log para poder buscarlos."""

    def filter(self, record: logging.LogRecord) -> bool:
        contexto = trace.get_current_span().get_span_context()
        record.trace_id = format(contexto.trace_id, "032x") if contexto.is_valid else "-"
        record.span_id = format(contexto.span_id, "016x") if contexto.is_valid else "-"
        return True


def setup_telemetry(settings: Settings) -> Telemetry | None:
    """Sin efecto si ``settings.otel_enabled`` es falso (local y pruebas)."""
    if not settings.otel_enabled:
        return None

    resource = Resource.create(
        {"service.name": settings.service_name, "deployment.environment": settings.environment}
    )
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    handler.addFilter(_AtributosDeTraza())
    handler.setFormatter(logging.Formatter("%(message)s trace_id=%(trace_id)s span_id=%(span_id)s"))
    logging.getLogger().addHandler(handler)

    # La llamada a Resend queda como span hijo del envío.
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)
    return tracer_provider, logger_provider


def vaciar(telemetry: Telemetry | None) -> None:
    """Fuerza el envío de lo pendiente. En Cloud Run la CPU se reduce al terminar la
    petición: sin esto, los spans y logs del último envío pueden perderse."""
    if telemetry is None:
        return
    for provider in telemetry:
        provider.force_flush(_FLUSH_MS)


def shutdown_telemetry(telemetry: Telemetry | None) -> None:
    if telemetry is None:
        return
    for provider in telemetry:
        provider.shutdown()
