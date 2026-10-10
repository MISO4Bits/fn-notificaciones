"""Sobre de las suscripciones *push* de Pub/Sub. Refleja ``openapi/openapi.yaml``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class MensajePubSub(_Model):
    data: str = ""
    attributes: dict[str, str] = Field(default_factory=dict)
    message_id: str = ""
    publish_time: str | None = None


class SobrePush(_Model):
    message: MensajePubSub
    subscription: str | None = None
    # Solo viene si la suscripción tiene cola de mensajes fallidos (la nuestra la tiene).
    delivery_attempt: int | None = None
