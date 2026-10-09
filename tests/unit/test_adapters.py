from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.adapters.factory import build_sender
from app.adapters.logging_sender import LoggingEmailSender
from app.adapters.resend_sender import URL, ResendEmailSender
from app.config import Settings
from app.domain import Correo, EnvioRechazado, EnvioTransitorio

CORREO = Correo(
    id="evento-1",
    destinatario="ana.rios@example.com",
    asunto="Hola Ana",
    cuerpo_html="<p>Hola Ana</p>",
    cuerpo_texto="Hola Ana",
    plantilla="bienvenida",
)
REMITENTE = "Solventa <no-reply@notificaciones.solventa4bits.com>"


def _sender(handler, reply_to="soporte@solventa4bits.com") -> ResendEmailSender:
    cliente = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ResendEmailSender("re_clave_de_prueba", REMITENTE, reply_to, client=cliente)


async def test_resend_envia_el_correo_con_sus_encabezados():
    pedidos: list[httpx.Request] = []

    def handler(request):
        pedidos.append(request)
        return httpx.Response(200, json={"id": "abc"})

    sender = _sender(handler)
    await sender.enviar(CORREO)

    pedido = pedidos[0]
    assert str(pedido.url) == URL
    assert pedido.headers["Authorization"] == "Bearer re_clave_de_prueba"
    assert pedido.headers["Idempotency-Key"] == "evento-1"
    assert json.loads(pedido.content) == {
        "from": REMITENTE,
        "to": ["ana.rios@example.com"],
        "subject": "Hola Ana",
        "html": "<p>Hola Ana</p>",
        "text": "Hola Ana",
        "reply_to": "soporte@solventa4bits.com",
    }
    await sender.aclose()


async def test_resend_sin_reply_to_no_lo_envia():
    pedidos: list[httpx.Request] = []

    def handler(request):
        pedidos.append(request)
        return httpx.Response(200, json={})

    await _sender(handler, reply_to=None).enviar(CORREO)

    assert "reply_to" not in json.loads(pedidos[0].content)


@pytest.mark.parametrize("estado", [500, 502, 503, 429, 401, 403])
async def test_resend_fallos_transitorios_o_de_configuracion_se_reintentan(estado):
    sender = _sender(lambda _r: httpx.Response(estado, json={"message": "ana.rios@example.com"}))

    with pytest.raises(EnvioTransitorio) as error:
        await sender.enviar(CORREO)

    assert str(estado) in str(error.value)
    assert "ana.rios" not in str(error.value)  # el cuerpo del error no se propaga


@pytest.mark.parametrize("estado", [400, 404, 409, 422])
async def test_resend_rechazos_no_se_reintentan(estado):
    sender = _sender(lambda _r: httpx.Response(estado, json={"message": "ana.rios@example.com"}))

    with pytest.raises(EnvioRechazado) as error:
        await sender.enviar(CORREO)

    assert str(estado) in str(error.value)
    assert "ana.rios" not in str(error.value)


async def test_resend_falla_de_red_es_transitoria():
    def handler(request):
        raise httpx.ConnectError("sin red")

    with pytest.raises(EnvioTransitorio, match="no está disponible"):
        await _sender(handler).enviar(CORREO)


async def test_resend_crea_su_propio_cliente_si_no_se_le_pasa_uno():
    sender = ResendEmailSender("re_clave", REMITENTE, timeout_s=1.0)
    await sender.aclose()


async def test_el_envio_simulado_no_registra_datos_personales(caplog):
    with caplog.at_level(logging.INFO):
        await LoggingEmailSender().enviar(CORREO)

    assert "plantilla=bienvenida" in caplog.text
    assert "ana.rios" not in caplog.text
    assert "Hola Ana" not in caplog.text


def test_la_fabrica_construye_cada_backend():
    assert isinstance(build_sender(Settings(sender_backend="logging")), LoggingEmailSender)
    sender = build_sender(Settings(sender_backend="resend", resend_api_key="re_clave"))
    assert isinstance(sender, ResendEmailSender)


def test_resend_exige_la_clave_de_api():
    with pytest.raises(ValueError, match="FN_RESEND_API_KEY"):
        build_sender(Settings(sender_backend="resend"))


def test_la_fabrica_rechaza_un_backend_desconocido():
    with pytest.raises(ValueError, match="no soportado"):
        build_sender(Settings(sender_backend="paloma"))
