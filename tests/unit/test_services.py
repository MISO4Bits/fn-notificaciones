from __future__ import annotations

import json

import pytest

from app.domain import EnvioRechazado, EnvioTransitorio, MensajeInvalido
from app.services import NotificacionesService
from tests.conftest import EVENTO_VALIDO, SenderEspia

CONTENIDO = json.dumps(EVENTO_VALIDO).encode()
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


def _servicio(sender, esperas: list[float] | None = None, intentos: int = 3):
    async def _dormir(segundos: float) -> None:
        if esperas is not None:
            esperas.append(segundos)

    return NotificacionesService(sender, intentos=intentos, dormir=_dormir)


async def test_envia_el_correo_del_evento():
    sender = SenderEspia()

    enviado = await _servicio(sender).procesar(CONTENIDO, {}, "m-1")

    assert enviado is True
    assert [c.destinatario for c in sender.enviados] == ["ana.rios@example.com"]
    assert sender.enviados[0].id == "evento-1"


async def test_ignora_un_evento_de_otro_tipo():
    sender = SenderEspia()
    otro = json.dumps({**EVENTO_VALIDO, "tipo": "ClienteRegistrado"}).encode()

    assert await _servicio(sender).procesar(otro, {}, "m-1") is False
    assert sender.enviados == []


async def test_reintenta_ante_un_fallo_transitorio_y_se_recupera():
    sender = SenderEspia(EnvioTransitorio("503"), EnvioTransitorio("503"))
    esperas: list[float] = []

    assert await _servicio(sender, esperas).procesar(CONTENIDO, {}, "m-1") is True

    assert len(sender.enviados) == 1
    assert esperas == [0.2, 0.4]  # espera creciente entre intentos


async def test_agota_los_intentos_y_deja_que_pubsub_reentregue():
    sender = SenderEspia(*[EnvioTransitorio("503")] * 3)
    esperas: list[float] = []

    with pytest.raises(EnvioTransitorio):
        await _servicio(sender, esperas).procesar(CONTENIDO, {}, "m-1")

    assert sender.enviados == []
    assert len(esperas) == 2  # no espera después del último intento


async def test_un_rechazo_del_proveedor_no_se_reintenta():
    sender = SenderEspia(EnvioRechazado("422"), None)
    esperas: list[float] = []

    with pytest.raises(EnvioRechazado):
        await _servicio(sender, esperas).procesar(CONTENIDO, {}, "m-1")

    assert sender.enviados == []
    assert esperas == []


async def test_un_mensaje_invalido_no_llega_al_proveedor():
    sender = SenderEspia()

    with pytest.raises(MensajeInvalido):
        await _servicio(sender).procesar(b"no es json", {}, "m-1")

    assert sender.enviados == []


@pytest.mark.parametrize("intentos", [0, -2])
async def test_siempre_hay_al_menos_un_intento(intentos):
    sender = SenderEspia()

    await _servicio(sender, intentos=intentos).procesar(CONTENIDO, {}, "m-1")

    assert len(sender.enviados) == 1


async def test_el_envio_ocurre_dentro_del_trace_de_los_atributos_del_mensaje():
    from opentelemetry import trace

    vistos: list[int] = []

    class Espia(SenderEspia):
        async def enviar(self, correo):
            vistos.append(trace.get_current_span().get_span_context().trace_id)
            await super().enviar(correo)

    await _servicio(Espia()).procesar(CONTENIDO, {"traceparent": TRACEPARENT}, "m-1")

    assert vistos == [0x0AF7651916CD43DD8448EB211C80319C]
