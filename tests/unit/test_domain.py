from __future__ import annotations

import base64
import json

import pytest

from app.domain import MensajeInvalido, decodificar, parsear_evento
from tests.conftest import EVENTO_VALIDO


def _evento(**datos) -> bytes:
    evento = {**EVENTO_VALIDO, "datos": {**EVENTO_VALIDO["datos"], **datos}}
    return json.dumps(evento).encode()


def test_decodificar_base64_valido():
    assert decodificar(base64.b64encode(b"hola").decode()) == b"hola"


@pytest.mark.parametrize("malo", ["no es base64!", "abc", "ab=c"])
def test_decodificar_base64_invalido(malo):
    with pytest.raises(MensajeInvalido):
        decodificar(malo)


def test_parsear_evento_valido():
    correo = parsear_evento(_evento(), "mensaje-9")

    assert correo is not None
    assert correo.id == "evento-1"
    assert correo.destinatario == "ana.rios@example.com"
    assert correo.asunto == "Hola Ana"
    assert correo.cuerpo_html == "<p>Hola Ana</p>"
    assert correo.cuerpo_texto == "Hola Ana"
    assert correo.plantilla == "bienvenida"
    assert correo.cliente_id == "cliente-1"


def test_el_id_del_mensaje_reemplaza_al_del_evento_si_este_falta():
    evento = {k: v for k, v in EVENTO_VALIDO.items() if k != "id"}
    assert parsear_evento(json.dumps(evento).encode(), "mensaje-9").id == "mensaje-9"


def test_un_evento_de_otro_tipo_se_ignora():
    evento = {**EVENTO_VALIDO, "tipo": "ClienteRegistrado"}
    assert parsear_evento(json.dumps(evento).encode(), "m") is None


def test_los_campos_nuevos_no_rompen_el_contrato():
    assert parsear_evento(_evento(campoNuevo="x"), "m") is not None


def test_cliente_id_es_opcional():
    datos = {k: v for k, v in EVENTO_VALIDO["datos"].items() if k != "clienteId"}
    evento = {**EVENTO_VALIDO, "datos": datos}
    assert parsear_evento(json.dumps(evento).encode(), "m").cliente_id is None


@pytest.mark.parametrize(
    "cambios",
    [
        {"destinatario": "no-es-un-correo"},
        {"destinatario": "ana@example.com\nbcc: otro@malo.co"},
        {"asunto": ""},
        {"asunto": "Hola\nBcc: otro@malo.co"},
        {"asunto": "x" * 999},
        {"cuerpoHtml": ""},
        {"cuerpoTexto": ""},
        {"plantilla": ""},
    ],
)
def test_datos_invalidos_se_rechazan(cambios):
    with pytest.raises(MensajeInvalido):
        parsear_evento(_evento(**cambios), "m")


@pytest.mark.parametrize("faltante", ["destinatario", "asunto", "cuerpoHtml", "cuerpoTexto"])
def test_falta_un_dato_obligatorio(faltante):
    datos = {k: v for k, v in EVENTO_VALIDO["datos"].items() if k != faltante}
    evento = {**EVENTO_VALIDO, "datos": datos}
    with pytest.raises(MensajeInvalido, match=faltante):
        parsear_evento(json.dumps(evento).encode(), "m")


@pytest.mark.parametrize(
    "contenido", [b"no es json", b"\xff\xfe", b"[]", b'{"tipo": "EnviarCorreo"}']
)
def test_contenido_ilegible_se_rechaza(contenido):
    with pytest.raises(MensajeInvalido):
        parsear_evento(contenido, "m")


def test_el_motivo_del_rechazo_no_repite_datos_personales():
    with pytest.raises(MensajeInvalido) as error:
        parsear_evento(_evento(destinatario="persona-privada-sin-arroba"), "m")
    assert "persona-privada" not in str(error.value)
    assert "destinatario" in str(error.value)


# --- adjuntos (BITS-101) ---

ADJUNTO = {
    "nombre": "Poliza-SOL-VH-2026-K7M2Q9.pdf",
    "bucket": "solventa-dev-polizas",
    "objeto": "polizas/7c0f2a52.pdf",
    "contentType": "application/pdf",
}


def test_un_evento_sin_adjuntos_no_trae_referencias():
    correo = parsear_evento(_evento(), "m")

    assert correo.referencias_adjuntos == ()
    assert correo.adjuntos == ()


def test_las_referencias_de_adjuntos_se_leen_del_evento():
    correo = parsear_evento(_evento(adjuntos=[ADJUNTO]), "m")

    [ref] = correo.referencias_adjuntos
    assert (ref.nombre, ref.bucket, ref.objeto, ref.content_type) == (
        "Poliza-SOL-VH-2026-K7M2Q9.pdf",
        "solventa-dev-polizas",
        "polizas/7c0f2a52.pdf",
        "application/pdf",
    )


def test_el_tipo_del_adjunto_es_pdf_si_no_se_indica():
    sin_tipo = {k: v for k, v in ADJUNTO.items() if k != "contentType"}

    ref = parsear_evento(_evento(adjuntos=[sin_tipo]), "m").referencias_adjuntos[0]

    assert ref.content_type == "application/pdf"


@pytest.mark.parametrize(
    "cambio",
    [
        {"nombre": "../secreto.pdf"},
        {"nombre": "a/b.pdf"},
        {"nombre": ""},
        {"nombre": "x\r\ny.pdf"},
        {"bucket": "Bucket Con Espacios"},
        {"bucket": "ab"},
        {"objeto": ""},
        {"objeto": "a\nb"},
    ],
)
def test_un_adjunto_mal_formado_invalida_el_mensaje(cambio):
    with pytest.raises(MensajeInvalido):
        parsear_evento(_evento(adjuntos=[{**ADJUNTO, **cambio}]), "m")


def test_no_se_admiten_mas_de_tres_adjuntos():
    with pytest.raises(MensajeInvalido):
        parsear_evento(_evento(adjuntos=[ADJUNTO] * 4), "m")
