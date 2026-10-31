"""BITS-229: la suscripción push de avisos (``POST /push``): qué responde la función."""

from __future__ import annotations

import logging

import pytest

from app.domain import EnvioRechazado, EnvioTransitorio, TokenNoRegistrado
from app.ports import PushSenderPort
from app.services import PushService
from tests.conftest import EVENTO_VALIDO, sobre_push
from tests.unit.test_push import EVENTO, TOKEN_A, TOKEN_B, Espia, _sin_espera


@pytest.fixture
def espia() -> Espia:
    return Espia()


@pytest.fixture(autouse=True)
def _con_espia(app, espia):
    app.state.push = PushService(espia, intentos=3, dormir=_sin_espera)


def _sobre(evento=EVENTO) -> dict:
    return sobre_push(
        evento,
        atributos={"tipo": "EnviarPush", "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"},
    )


async def test_un_aviso_valido_se_entrega_y_se_confirma(client, espia):
    resp = await client.post("/push", json=_sobre())

    assert resp.status_code == 204
    assert resp.content == b""
    assert sorted(t for _, t in espia.enviados) == [TOKEN_A, TOKEN_B]


async def test_un_evento_de_otro_tipo_se_confirma_sin_entregar(client, espia):
    resp = await client.post("/push", json=_sobre(EVENTO_VALIDO))

    assert resp.status_code == 204
    assert espia.enviados == []


async def test_un_token_muerto_se_confirma_y_los_demas_reciben(app, client):
    espia = Espia(**{TOKEN_A: [TokenNoRegistrado("404 UNREGISTERED")]})
    app.state.push = PushService(espia, intentos=3, dormir=_sin_espera)

    resp = await client.post("/push", json=_sobre())

    assert resp.status_code == 204
    assert [t for _, t in espia.enviados] == [TOKEN_B]


async def test_un_rechazo_definitivo_se_confirma(app, client):
    espia = Espia(
        **{TOKEN_A: [EnvioRechazado("400 INVALID_ARGUMENT")], TOKEN_B: [EnvioRechazado("400")]}
    )
    app.state.push = PushService(espia, intentos=3, dormir=_sin_espera)

    resp = await client.post("/push", json=_sobre())

    assert resp.status_code == 204
    assert espia.enviados == []


async def test_un_fallo_transitorio_agotado_devuelve_503(app, client):
    espia = Espia(**{TOKEN_A: [EnvioTransitorio("503")] * 3})
    app.state.push = PushService(espia, intentos=3, dormir=_sin_espera)

    resp = await client.post("/push", json=_sobre())

    assert resp.status_code == 503


async def test_un_mensaje_mal_formado_se_confirma_y_se_anota_sin_tokens(client, espia, caplog):
    malo = {**EVENTO, "datos": {**EVENTO["datos"], "titulo": ""}}

    with caplog.at_level(logging.ERROR):
        resp = await client.post("/push", json=_sobre(malo))

    assert resp.status_code == 204
    assert espia.enviados == []
    assert "mensaje descartado" in caplog.text
    assert TOKEN_A not in caplog.text


async def test_un_sobre_que_no_es_de_pubsub_se_confirma(client, espia):
    resp = await client.post("/push", json={"algo": "distinto"})

    assert resp.status_code == 204
    assert espia.enviados == []


async def test_el_aviso_viaja_con_su_id_y_destino(client, espia):
    await client.post("/push", json=_sobre())

    assert {i for i, _ in espia.enviados} == {"evento-push-1"}


def test_el_espia_es_un_puerto_valido(espia):
    assert isinstance(espia, PushSenderPort)
