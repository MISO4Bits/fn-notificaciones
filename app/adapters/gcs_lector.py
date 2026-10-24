"""Lee de Cloud Storage los archivos que un correo lleva adjuntos.

La cuenta de servicio de la función necesita permiso de lectura sobre el bucket de adjuntos
(``roles/storage.objectViewer``). Fuera del alcance de las pruebas locales (requiere un bucket
real): se excluye de la medición de cobertura.
"""

from __future__ import annotations

import asyncio
import logging

from google.api_core.exceptions import GoogleAPICallError, NotFound
from google.cloud import storage

from app.domain import Adjunto, EnvioRechazado, EnvioTransitorio, ReferenciaAdjunto

logger = logging.getLogger("fn_notificaciones.adapters.gcs")


class LectorGcs:  # pragma: no cover
    def __init__(self) -> None:
        self._cliente = storage.Client()

    async def leer(self, referencia: ReferenciaAdjunto, max_bytes: int) -> Adjunto:
        blob = self._cliente.bucket(referencia.bucket).blob(referencia.objeto)

        def _leer() -> bytes:
            blob.reload()  # trae el tamaño sin descargar el archivo
            if blob.size is not None and blob.size > max_bytes:
                raise EnvioRechazado("el adjunto supera el tamaño permitido")
            return blob.download_as_bytes()

        try:
            contenido = await asyncio.to_thread(_leer)
        except NotFound as exc:
            # Puede que el archivo todavía no aparezca: se reentrega en vez de perder el correo.
            logger.warning("adjunto no encontrado todavía nombre=%s", referencia.nombre)
            raise EnvioTransitorio("el adjunto todavía no está en el bucket") from exc
        except GoogleAPICallError as exc:
            raise EnvioTransitorio("el bucket de adjuntos no responde") from exc
        return Adjunto(referencia.nombre, referencia.content_type, contenido)
