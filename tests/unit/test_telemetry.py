from __future__ import annotations

import logging

import opentelemetry.trace as otel_trace_module
import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk._logs.export import InMemoryLogExporter
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.util._once import Once

from app import telemetry
from app.config import Settings
from app.telemetry import _AtributosDeTraza, setup_telemetry, shutdown_telemetry, vaciar


class _ExportadorDeSpans:
    def __init__(self) -> None:
        self.spans: list = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


@pytest.fixture(autouse=True)
def _aislado():
    """El TracerProvider global solo se puede fijar una vez por proceso, y la
    instrumentación de httpx y el handler de logs son de todo el proceso: se
    restablece todo al terminar para no contaminar otras pruebas."""
    handlers = list(logging.getLogger().handlers)
    otel_trace_module._TRACER_PROVIDER = None
    otel_trace_module._TRACER_PROVIDER_SET_ONCE = Once()
    token = otel_context.attach(otel_context.Context())
    try:
        yield
    finally:
        otel_context.detach(token)
        HTTPXClientInstrumentor().uninstrument()
        root = logging.getLogger()
        for handler in list(root.handlers):
            if handler not in handlers:
                root.removeHandler(handler)
        otel_trace_module._TRACER_PROVIDER = None
        otel_trace_module._TRACER_PROVIDER_SET_ONCE = Once()


def test_deshabilitado_no_hace_nada():
    assert setup_telemetry(Settings(otel_enabled=False)) is None
    vaciar(None)
    shutdown_telemetry(None)


def test_habilitado_exporta_los_spans_y_los_logs_con_el_trace_id(monkeypatch):
    spans = _ExportadorDeSpans()
    logs = InMemoryLogExporter()
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", lambda: spans)
    monkeypatch.setattr(telemetry, "OTLPLogExporter", lambda: logs)

    proveedores = setup_telemetry(Settings(otel_enabled=True, environment="prueba"))
    assert proveedores is not None

    with trace.get_tracer("prueba").start_as_current_span("envio") as span:
        logging.getLogger("fn_notificaciones.prueba").warning("correo enviado plantilla=x")
        esperado = format(span.get_span_context().trace_id, "032x")

    vaciar(proveedores)
    assert [s.name for s in spans.spans] == ["envio"]
    assert spans.spans[0].resource.attributes["service.name"] == "fn-notificaciones"
    assert spans.spans[0].resource.attributes["deployment.environment"] == "prueba"
    cuerpos = [r.log_record.body for r in logs.get_finished_logs()]
    assert any(f"trace_id={esperado}" in str(c) for c in cuerpos)
    shutdown_telemetry(proveedores)


def test_el_filtro_de_logs_sin_span_activo_marca_guiones():
    registro = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)

    assert _AtributosDeTraza().filter(registro) is True
    assert (registro.trace_id, registro.span_id) == ("-", "-")
