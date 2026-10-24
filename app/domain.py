"""Modelo de dominio: el correo que hay que enviar y los errores del envío.

La función no decide nada: el mensaje llega completo (asunto y cuerpos ya
rellenados por quien lo publica) y solo se entrega al proveedor de correo.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

EVENTO_ENVIAR_CORREO = "EnviarCorreo"

_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class MensajeInvalido(Exception):
    """El mensaje de Pub/Sub no tiene la forma esperada. Reintentarlo no lo arregla."""


class EnvioRechazado(Exception):
    """El proveedor rechazó este correo. Reintentarlo no lo arregla."""


class EnvioTransitorio(Exception):
    """El envío falló por algo que puede resolverse solo (red, límite, proveedor caído)."""


@dataclass(frozen=True)
class ReferenciaAdjunto:
    """Un archivo que ya está en el bucket y debe viajar adjunto (p. ej. el PDF de la póliza)."""

    nombre: str
    bucket: str
    objeto: str
    content_type: str


@dataclass(frozen=True)
class Adjunto:
    nombre: str
    content_type: str
    contenido: bytes


@dataclass(frozen=True)
class Correo:
    id: str
    destinatario: str
    asunto: str
    cuerpo_html: str
    cuerpo_texto: str
    plantilla: str
    cliente_id: str | None = None
    # Lo que pide el evento: referencias a archivos del bucket. El servicio las lee y deja
    # los bytes en ``adjuntos``, que es lo único que ve el proveedor de correo.
    referencias_adjuntos: tuple[ReferenciaAdjunto, ...] = ()
    adjuntos: tuple[Adjunto, ...] = ()


class _AdjuntoIn(BaseModel):
    """Un adjunto del evento: dónde está el archivo y cómo se llamará en el correo."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    # Solo el nombre del archivo: sin rutas ni caracteres de control.
    nombre: str = Field(min_length=1, max_length=120, pattern=r"^[\w][\w .()-]*$")
    bucket: str = Field(min_length=3, max_length=222, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    objeto: str = Field(min_length=1, max_length=1024, pattern=r"^[^\r\n]+$")
    content_type: str = Field(default="application/pdf", max_length=100)


class _Datos(BaseModel):
    """``datos`` del evento ``EnviarCorreo`` (contrato con CoreTransaccional)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    destinatario: str = Field(max_length=254, pattern=_EMAIL)
    asunto: str = Field(min_length=1, max_length=998, pattern=r"^[^\r\n]+$")
    cuerpo_html: str = Field(min_length=1)
    cuerpo_texto: str = Field(min_length=1)
    plantilla: str = Field(min_length=1, max_length=64)
    cliente_id: str | None = Field(default=None, max_length=64)
    adjuntos: list[_AdjuntoIn] = Field(default_factory=list, max_length=3)


class _Evento(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    tipo: str
    id: str | None = None
    datos: _Datos


def decodificar(data_base64: str) -> bytes:
    try:
        return base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MensajeInvalido("El cuerpo del mensaje no es base64 válido") from exc


def parsear_evento(contenido: bytes, id_mensaje: str) -> Correo | None:
    """Convierte el cuerpo del mensaje en un ``Correo``.

    Devuelve ``None`` si el evento es de otro tipo (no es para esta función).
    Lanza ``MensajeInvalido`` si le faltan datos o no se puede leer; el motivo no
    incluye valores del mensaje, que traen datos personales.
    """
    try:
        evento = _Evento.model_validate(json.loads(contenido))
    except (ValueError, UnicodeDecodeError) as exc:
        # ValidationError es un ValueError: se distingue solo para el texto del motivo.
        motivo = _campos(exc) if isinstance(exc, ValidationError) else "no es JSON"
        raise MensajeInvalido(f"El evento no es válido ({motivo})") from None
    if evento.tipo != EVENTO_ENVIAR_CORREO:
        return None
    d = evento.datos
    return Correo(
        id=evento.id or id_mensaje,
        destinatario=d.destinatario,
        asunto=d.asunto,
        cuerpo_html=d.cuerpo_html,
        cuerpo_texto=d.cuerpo_texto,
        plantilla=d.plantilla,
        cliente_id=d.cliente_id,
        referencias_adjuntos=tuple(
            ReferenciaAdjunto(a.nombre, a.bucket, a.objeto, a.content_type) for a in d.adjuntos
        ),
    )


def _campos(error: ValidationError) -> str:
    return ", ".join(sorted({".".join(str(p) for p in e["loc"]) for e in error.errors()}))
