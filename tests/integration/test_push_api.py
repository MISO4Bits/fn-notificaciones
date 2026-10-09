"""La suscripción push: qué responde la función y, por tanto, qué hace Pub/Sub."""

from __future__ import annotations

import logging

from tests.conftest import EVENTO_VALIDO, EnvioRechazado, EnvioTransitorio, SenderEspia, sobre_push


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "fn-notificaciones"}


async def test_un_mensaje_valido_se_envia_y_se_confirma(client, sender):
    resp = await client.post("/", json=sobre_push(EVENTO_VALIDO))

    assert resp.status_code == 204
    assert resp.content == b""
    assert [c.destinatario for c in sender.enviados] == ["ana.rios@example.com"]


async def test_un_evento_de_otro_tipo_se_confirma_sin_enviar(client, sender):
    evento = {**EVENTO_VALIDO, "tipo": "ClienteRegistrado"}

    resp = await client.post("/", json=sobre_push(evento))

    assert resp.status_code == 204
    assert sender.enviados == []


async def test_un_fallo_transitorio_agotado_devuelve_503_para_que_pubsub_reentregue(app, client):
    app.state.service._sender = SenderEspia(*[EnvioTransitorio("503")] * 3)

    resp = await client.post("/", json=sobre_push(EVENTO_VALIDO))

    assert resp.status_code == 503


async def test_un_fallo_transitorio_que_se_recupera_se_confirma(app, client):
    espia = SenderEspia(EnvioTransitorio("503"))
    app.state.service._sender = espia

    resp = await client.post("/", json=sobre_push(EVENTO_VALIDO))

    assert resp.status_code == 204
    assert len(espia.enviados) == 1


async def test_un_rechazo_del_proveedor_se_confirma_y_se_anota(app, client, caplog):
    app.state.service._sender = SenderEspia(EnvioRechazado("Resend rechazó el correo (422)"))

    with caplog.at_level(logging.ERROR):
        resp = await client.post("/", json=sobre_push(EVENTO_VALIDO))

    assert resp.status_code == 204
    assert "mensaje descartado" in caplog.text
    assert "ana.rios" not in caplog.text


async def test_un_mensaje_mal_formado_se_confirma_y_se_anota_sin_datos(client, sender, caplog):
    malo = {**EVENTO_VALIDO, "datos": {**EVENTO_VALIDO["datos"], "destinatario": "sin-arroba"}}

    with caplog.at_level(logging.ERROR):
        resp = await client.post("/", json=sobre_push(malo))

    assert resp.status_code == 204
    assert sender.enviados == []
    assert "mensaje descartado" in caplog.text
    assert "destinatario" in caplog.text
    assert "sin-arroba" not in caplog.text


async def test_un_cuerpo_que_no_es_base64_se_confirma(client, sender):
    sobre = sobre_push(EVENTO_VALIDO)
    sobre["message"]["data"] = "esto no es base64!"

    resp = await client.post("/", json=sobre)

    assert resp.status_code == 204
    assert sender.enviados == []


async def test_una_solicitud_que_no_es_un_sobre_de_pubsub_se_confirma(client, sender, caplog):
    with caplog.at_level(logging.ERROR):
        resp = await client.post("/", json={"cualquier": "cosa"})

    assert resp.status_code == 204
    assert resp.content == b""
    assert sender.enviados == []
    assert "no es un sobre de Pub/Sub" in caplog.text


async def test_el_trace_de_los_atributos_llega_al_envio(app, client):
    from opentelemetry import trace

    vistos: list[int] = []

    class Espia(SenderEspia):
        async def enviar(self, correo):
            vistos.append(trace.get_current_span().get_span_context().trace_id)

    app.state.service._sender = Espia()
    traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"

    await client.post("/", json=sobre_push(EVENTO_VALIDO, atributos={"traceparent": traceparent}))

    assert vistos == [0x0AF7651916CD43DD8448EB211C80319C]


async def test_al_apagar_se_cierra_el_proveedor_de_correo(settings, monkeypatch):
    from app.api.app import create_app

    cerrado = []

    class Cerrable(SenderEspia):
        async def aclose(self):
            cerrado.append(True)

    monkeypatch.setattr("app.api.app.build_sender", lambda _settings: Cerrable())
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        pass

    assert cerrado == [True]
