"""La implementación cumple ``openapi/openapi.yaml`` y entiende el evento que publica Core."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from app.domain import parsear_evento
from tests.conftest import sobre_push

_METODOS = {"get", "post", "put", "patch", "delete"}


def _validar(spec: dict, esquema: str, instancia) -> None:
    schema = {"$ref": f"#/components/schemas/{esquema}", "components": spec["components"]}
    errores = sorted(Draft202012Validator(schema).iter_errors(instancia), key=str)
    assert not errores, f"{esquema}: {[e.message for e in errores]}"


def test_las_operaciones_del_contrato_son_las_de_la_aplicacion(app, openapi_spec):
    contrato = {
        (metodo.upper(), ruta)
        for ruta, item in openapi_spec["paths"].items()
        for metodo in item
        if metodo.lower() in _METODOS
    }
    codigo = {
        (metodo.upper(), ruta)
        for ruta, item in app.openapi()["paths"].items()
        for metodo in item
        if metodo.lower() in _METODOS
    }
    assert contrato == codigo


def test_el_ejemplo_del_evento_cumple_su_esquema_y_la_funcion_lo_entiende(openapi_spec):
    ejemplo = openapi_spec["components"]["schemas"]["EventoEnviarCorreo"]["examples"][0]

    _validar(openapi_spec, "EventoEnviarCorreo", ejemplo)
    correo = parsear_evento(json.dumps(ejemplo).encode(), "m")
    assert correo is not None
    assert correo.destinatario == ejemplo["datos"]["destinatario"]
    assert correo.id == ejemplo["id"]


def test_el_sobre_de_pubsub_cumple_el_esquema(openapi_spec):
    ejemplo = openapi_spec["components"]["schemas"]["EventoEnviarCorreo"]["examples"][0]
    _validar(openapi_spec, "SobrePush", sobre_push(ejemplo))


async def test_las_respuestas_del_contrato_son_las_que_da_la_aplicacion(
    client, openapi_spec, sender
):
    documentadas = set(openapi_spec["paths"]["/"]["post"]["responses"])
    ejemplo = openapi_spec["components"]["schemas"]["EventoEnviarCorreo"]["examples"][0]

    resp = await client.post("/", json=sobre_push(ejemplo))

    assert str(resp.status_code) in documentadas
    assert len(sender.enviados) == 1


async def test_se_expone_el_contrato(client):
    resp = await client.get("/openapi.yaml")
    assert resp.status_code == 200
    assert "fn-notificaciones" in resp.text
