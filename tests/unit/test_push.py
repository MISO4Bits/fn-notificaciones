"""BITS-229: avisos push — lectura del evento, entrega por dispositivo y adaptador de Firebase."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.adapters.factory import build_push_sender
from app.adapters.fcm_sender import ALCANCE, FcmPushSender
from app.adapters.logging_push import LoggingPushSender
from app.config import Settings
from app.domain import (
    Aviso,
    EnvioRechazado,
    EnvioTransitorio,
    MensajeInvalido,
    TokenNoRegistrado,
    parsear_aviso,
    token_corto,
)
from app.services import PushService, ResultadoPush

TOKEN_A = "fcm-token-del-telefono-aaaaaa"
TOKEN_B = "fcm-token-del-telefono-bbbbbb"
EVENTO = {
    "tipo": "EnviarPush",
    "id": "evento-push-1",
    "ocurridoEn": "2026-10-30T15:04:05Z",
    "datos": {
        "clienteId": "cliente-1",
        "tokens": [TOKEN_A, TOKEN_B],
        "titulo": "Tu póliza ya está activa",
        "cuerpo": "Ya puedes ver tu seguro y descargar el documento.",
        "ruta": {"destino": "seguro", "id": "poliza-1"},
        "prioridad": "alta",
    },
}


def _bytes(evento) -> bytes:
    return json.dumps(evento).encode()


def _con_datos(**cambios) -> dict:
    return {**EVENTO, "datos": {**EVENTO["datos"], **cambios}}


# --- lectura del evento ---


def test_el_evento_se_convierte_en_un_aviso():
    aviso = parsear_aviso(_bytes(EVENTO), "m1")

    assert aviso == Aviso(
        id="evento-push-1",
        tokens=(TOKEN_A, TOKEN_B),
        titulo="Tu póliza ya está activa",
        cuerpo="Ya puedes ver tu seguro y descargar el documento.",
        destino="seguro",
        destino_id="poliza-1",
        prioridad="alta",
        cliente_id="cliente-1",
    )


def test_sin_id_se_usa_el_del_mensaje_y_la_prioridad_es_alta_por_defecto():
    evento = {k: v for k, v in EVENTO.items() if k != "id"}
    evento["datos"] = {k: v for k, v in EVENTO["datos"].items() if k != "prioridad"}

    aviso = parsear_aviso(_bytes(evento), "mensaje-9")

    assert aviso.id == "mensaje-9"
    assert aviso.prioridad == "alta"


def test_los_tokens_repetidos_se_envian_una_vez():
    aviso = parsear_aviso(_bytes(_con_datos(tokens=[TOKEN_A, TOKEN_A, TOKEN_B])), "m")

    assert aviso.tokens == (TOKEN_A, TOKEN_B)


def test_un_evento_de_otro_tipo_se_ignora_aunque_no_tenga_la_forma_de_un_push():
    assert parsear_aviso(_bytes({"tipo": "EnviarCorreo", "datos": {"x": 1}}), "m") is None


@pytest.mark.parametrize(
    "malo",
    [
        _con_datos(tokens=[]),
        _con_datos(tokens=[f"token-numero-{i}" for i in range(6)]),
        _con_datos(tokens=["token con espacio"]),
        _con_datos(titulo=""),
        _con_datos(titulo="dos\nlíneas"),
        _con_datos(cuerpo="x" * 241),
        _con_datos(ruta={"destino": "siniestros", "id": "1"}),
        _con_datos(ruta={"destino": "seguro", "id": ""}),
        _con_datos(prioridad="urgente"),
        {"tipo": "EnviarPush", "id": "x"},
    ],
)
def test_un_evento_mal_formado_es_invalido(malo):
    with pytest.raises(MensajeInvalido):
        parsear_aviso(_bytes(malo), "m")


def test_lo_que_no_es_json_es_invalido_y_el_motivo_no_trae_el_token():
    with pytest.raises(MensajeInvalido, match="no es JSON"):
        parsear_aviso(b"no es json", "m")
    with pytest.raises(MensajeInvalido) as capturado:
        parsear_aviso(_bytes(_con_datos(titulo="")), "m")
    assert TOKEN_A not in str(capturado.value)


def test_del_token_solo_se_ve_el_final():
    assert token_corto(TOKEN_A) == "…aaaaaa"


# --- servicio ---


class Espia:
    """Doble del puerto: guarda lo entregado y falla por token a pedido."""

    def __init__(self, **errores: list[Exception]) -> None:
        self.enviados: list[tuple[str, str]] = []
        self._errores = {k: list(v) for k, v in errores.items()}

    async def enviar(self, aviso: Aviso, token: str) -> None:
        pendientes = self._errores.get(token)
        if pendientes:
            raise pendientes.pop(0)
        self.enviados.append((aviso.id, token))


async def _sin_espera(_segundos: float) -> None:
    return None


def _servicio(espia, **kw) -> PushService:
    return PushService(espia, intentos=3, dormir=_sin_espera, **kw)


async def test_se_entrega_a_cada_dispositivo():
    espia = Espia()

    resultado = await _servicio(espia).procesar(_bytes(EVENTO), {}, "m")

    assert resultado == ResultadoPush(enviados=2)
    assert sorted(t for _, t in espia.enviados) == [TOKEN_A, TOKEN_B]


async def test_un_mensaje_de_otro_tipo_devuelve_none():
    assert await _servicio(Espia()).procesar(_bytes({"tipo": "EnviarCorreo"}), {}, "m") is None


async def test_un_token_muerto_no_detiene_a_los_demas(caplog):
    espia = Espia(**{TOKEN_A: [TokenNoRegistrado("404 UNREGISTERED")]})

    with caplog.at_level(logging.INFO):
        resultado = await _servicio(espia).procesar(_bytes(EVENTO), {}, "m")

    assert resultado == ResultadoPush(enviados=1, tokens_muertos=1)
    assert espia.enviados == [("evento-push-1", TOKEN_B)]
    assert "token muerto" in caplog.text


async def test_un_rechazo_definitivo_se_anota_y_no_se_reintenta():
    espia = Espia(**{TOKEN_A: [EnvioRechazado("400 INVALID_ARGUMENT")] * 3})

    resultado = await _servicio(espia).procesar(_bytes(EVENTO), {}, "m")

    assert resultado == ResultadoPush(enviados=1, rechazados=1)


async def test_un_fallo_transitorio_que_se_recupera_se_entrega():
    espia = Espia(**{TOKEN_A: [EnvioTransitorio("503"), EnvioTransitorio("503")]})

    resultado = await _servicio(espia).procesar(_bytes(EVENTO), {}, "m")

    assert resultado == ResultadoPush(enviados=2)


async def test_si_un_dispositivo_agota_los_intentos_se_devuelve_a_pubsub():
    espia = Espia(**{TOKEN_A: [EnvioTransitorio("503")] * 3})

    with pytest.raises(EnvioTransitorio):
        await _servicio(espia).procesar(_bytes(EVENTO), {}, "m")

    # El otro dispositivo sí recibió el aviso; la reentrega lo reemplaza por la etiqueta.
    assert espia.enviados == [("evento-push-1", TOKEN_B)]


async def test_un_error_inesperado_se_propaga():
    espia = Espia(**{TOKEN_A: [RuntimeError("bug")]})

    with pytest.raises(RuntimeError):
        await _servicio(espia).procesar(_bytes(EVENTO), {}, "m")


async def test_la_concurrencia_se_limita():
    activos = 0
    maximo = 0

    class Lento:
        async def enviar(self, aviso, token):
            nonlocal activos, maximo
            activos += 1
            maximo = max(maximo, activos)
            import asyncio

            await asyncio.sleep(0.01)
            activos -= 1

    await _servicio(Lento(), concurrencia=2).procesar(
        _bytes(_con_datos(tokens=[f"token-numero-{i}" for i in range(5)])), {}, "m"
    )

    assert maximo == 2


# --- adaptador de Firebase ---


class _Credenciales:
    def __init__(self, token="acceso-1", valida=True) -> None:
        self.token = token
        self.valid = valida
        self.refrescos = 0

    def refresh(self, _request) -> None:
        self.refrescos += 1
        self.valid = True


AVISO = Aviso(
    id="evento-push-1",
    tokens=(TOKEN_A,),
    titulo="Tu póliza ya está activa",
    cuerpo="Ya puedes ver tu seguro y descargar el documento.",
    destino="seguro",
    destino_id="poliza-1",
    cliente_id="cliente-1",
)


def _fcm(handler, credenciales=None, **kw) -> FcmPushSender:
    cliente = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FcmPushSender(
        "solventa-dev", client=cliente, credenciales=credenciales or _Credenciales(), **kw
    )


async def test_fcm_manda_el_mensaje_con_los_datos_para_abrir_el_seguro():
    pedidos: list[httpx.Request] = []

    def handler(request):
        pedidos.append(request)
        return httpx.Response(200, json={"name": "projects/solventa-dev/messages/1"})

    await _fcm(handler).enviar(AVISO, TOKEN_A)

    pedido = pedidos[0]
    assert str(pedido.url) == "https://fcm.googleapis.com/v1/projects/solventa-dev/messages:send"
    assert pedido.headers["Authorization"] == "Bearer acceso-1"
    assert json.loads(pedido.content) == {
        "message": {
            "token": TOKEN_A,
            "notification": {
                "title": "Tu póliza ya está activa",
                "body": "Ya puedes ver tu seguro y descargar el documento.",
            },
            "data": {"destino": "seguro", "id": "poliza-1"},
            "android": {
                "priority": "HIGH",
                "ttl": "86400s",
                "notification": {"tag": "evento-push-1"},
            },
        }
    }


async def test_fcm_usa_prioridad_normal_y_el_ttl_configurado():
    cuerpos: list[dict] = []

    def handler(request):
        cuerpos.append(json.loads(request.content))
        return httpx.Response(200, json={})

    from dataclasses import replace

    await _fcm(handler, ttl_s=3600).enviar(replace(AVISO, prioridad="normal"), TOKEN_A)

    assert cuerpos[0]["message"]["android"]["priority"] == "NORMAL"
    assert cuerpos[0]["message"]["android"]["ttl"] == "3600s"


async def test_fcm_renueva_el_token_de_acceso_vencido():
    credenciales = _Credenciales(valida=False)

    await _fcm(lambda r: httpx.Response(200, json={}), credenciales).enviar(AVISO, TOKEN_A)

    assert credenciales.refrescos == 1


def _error(estado: int, codigo: str | None, *, en_detalle: bool = True) -> httpx.Response:
    error: dict = {"code": estado, "message": f"el token {TOKEN_A} falló", "status": "X"}
    if codigo and en_detalle:
        error["details"] = [
            {"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": codigo}
        ]
    elif codigo:
        error["status"] = codigo
    return httpx.Response(estado, json={"error": error})


@pytest.mark.parametrize(
    ("respuesta", "excepcion"),
    [
        (_error(404, "UNREGISTERED"), TokenNoRegistrado),
        (_error(400, "INVALID_ARGUMENT"), EnvioRechazado),
        (_error(403, "SENDER_ID_MISMATCH"), EnvioRechazado),
        (_error(429, "QUOTA_EXCEEDED"), EnvioTransitorio),
        (_error(503, "UNAVAILABLE"), EnvioTransitorio),
        (_error(500, None), EnvioTransitorio),
        # Sin permiso para la cuenta de servicio: configuración, no del aviso.
        (_error(403, "PERMISSION_DENIED", en_detalle=False), EnvioTransitorio),
        (_error(401, None), EnvioTransitorio),
        (httpx.Response(502, text="<html>"), EnvioTransitorio),
        (httpx.Response(500, json=["no es un objeto"]), EnvioTransitorio),
    ],
)
async def test_fcm_traduce_las_respuestas(respuesta, excepcion):
    with pytest.raises(excepcion) as capturado:
        await _fcm(lambda r: respuesta).enviar(AVISO, TOKEN_A)

    assert TOKEN_A not in str(capturado.value)


async def test_fcm_sin_respuesta_es_transitorio():
    def handler(request):
        raise httpx.ConnectError("sin red")

    with pytest.raises(EnvioTransitorio):
        await _fcm(handler).enviar(AVISO, TOKEN_A)


async def test_fcm_no_escribe_el_token_completo_en_los_logs(caplog):
    with caplog.at_level(logging.INFO):
        await _fcm(lambda r: httpx.Response(200, json={})).enviar(AVISO, TOKEN_A)
        with pytest.raises(TokenNoRegistrado):
            await _fcm(lambda r: _error(404, "UNREGISTERED")).enviar(AVISO, TOKEN_A)

    assert TOKEN_A not in caplog.text
    assert "…aaaaaa" in caplog.text


async def test_fcm_sin_credenciales_utilizables_es_transitorio(monkeypatch):
    from google.auth.exceptions import DefaultCredentialsError

    def sin_credenciales(**_kw):
        raise DefaultCredentialsError("sin credenciales")

    monkeypatch.setattr("google.auth.default", sin_credenciales)
    sender = FcmPushSender(
        "solventa-dev", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: None))
    )

    with pytest.raises(EnvioTransitorio):
        await sender.enviar(AVISO, TOKEN_A)


async def test_fcm_pide_las_credenciales_con_el_alcance_de_mensajeria(monkeypatch):
    pedidos: list[list[str]] = []
    credenciales = _Credenciales()

    def default(scopes):
        pedidos.append(scopes)
        return credenciales, "solventa-dev"

    monkeypatch.setattr("google.auth.default", default)
    sender = FcmPushSender(
        "solventa-dev",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )

    await sender.enviar(AVISO, TOKEN_A)
    await sender.enviar(AVISO, TOKEN_B)

    assert pedidos == [[ALCANCE]]  # se piden una vez y se reutilizan


async def test_fcm_cierra_su_cliente():
    sender = _fcm(lambda r: httpx.Response(200))

    await sender.aclose()

    assert sender._client.is_closed


# --- simulado y fábrica ---


async def test_el_simulado_solo_registra_y_sin_el_token_completo(caplog):
    with caplog.at_level(logging.INFO):
        await LoggingPushSender().enviar(AVISO, TOKEN_A)

    assert "push simulado" in caplog.text
    assert TOKEN_A not in caplog.text


def test_la_fabrica_elige_el_adaptador():
    assert isinstance(build_push_sender(Settings()), LoggingPushSender)
    assert isinstance(
        build_push_sender(Settings(push_backend="fcm", fcm_project_id="solventa-dev")),
        FcmPushSender,
    )
    with pytest.raises(ValueError, match="FN_FCM_PROJECT_ID"):
        build_push_sender(Settings(push_backend="fcm"))
    with pytest.raises(ValueError, match="no soportado"):
        build_push_sender(Settings(push_backend="otro"))
