# fn-notificaciones

Función serverless (Cloud Run) que **envía los correos electrónicos** de Solventa. Es la
función por evento del componente "Comunicaciones y Documentos".

La función **no decide nada**: recibe el evento `EnviarCorreo` con el asunto y los dos cuerpos
(HTML y texto) ya rellenados y los entrega al proveedor de correo. Quien publica el evento
(hoy CoreTransaccional) se encarga de la plantilla, que vive en Productos y Configuración de
Mercado. Contrato: [`openapi/openapi.yaml`](openapi/openapi.yaml).

- **Entrada:** una suscripción *push* de Pub/Sub (filtro `tipo = EnviarCorreo`) llama a `POST /`.
- **Salida:** [Resend](https://resend.com) por HTTPS, desde
  `Solventa <no-reply@notificaciones.solventa4bits.com>`.
- **Reintentos:** hasta 3 intentos por mensaje con espera creciente (BITS-119 AC-6). Si agota los
  intentos responde `503` y Pub/Sub lo reentrega. Un mensaje que nunca podrá enviarse (mal
  formado, rechazado por el proveedor) se descarta con un error en el log.
- **Duplicados:** el `id` del evento viaja a Resend como `Idempotency-Key`; una reentrega no manda
  el correo dos veces.
- **Trazabilidad:** extrae el `traceparent` de los atributos del mensaje; el envío queda en el
  mismo trace que el registro. Exporta por OTLP/HTTP directo a Grafana Cloud.
- **Datos personales:** nunca se registran el destinatario ni el contenido (BITS-82).

## Correr en local

Requiere Python 3.12+.

```bash
python -m venv .venv
source .venv/Scripts/activate      # Windows (Git Bash);  en Linux/Mac: source .venv/bin/activate
pip install -e ".[dev]"

uvicorn app.main:app --reload --port 8200
```

Por defecto **no envía nada** (`FN_SENDER_BACKEND=logging`): solo registra que habría enviado.
Prueba rápida con un mensaje como el de Pub/Sub:

```bash
EVENTO='{"tipo":"EnviarCorreo","id":"e-1","datos":{"destinatario":"ana@example.com","plantilla":"bienvenida","asunto":"Hola","cuerpoHtml":"<p>Hola</p>","cuerpoTexto":"Hola"}}'
curl -i -X POST http://localhost:8200/ -H 'content-type: application/json' \
  -d "{\"message\":{\"data\":\"$(printf %s "$EVENTO" | base64 -w0)\",\"messageId\":\"1\"}}"
```

### Variables de entorno (prefijo `FN_`)

| Variable | Default | Notas |
|---|---|---|
| `FN_SENDER_BACKEND` | `logging` | `logging` (simulado) \| `resend` |
| `FN_RESEND_API_KEY` | — | obligatoria con `resend`; sale de Secret Manager, nunca del repo |
| `FN_REMITENTE` | `Solventa <no-reply@notificaciones.solventa4bits.com>` | |
| `FN_REPLY_TO` | — | alias al que responde quien contesta el correo |
| `FN_PUSH_BACKEND` | `logging` | avisos push: `logging` (simulado) \| `fcm` (Firebase Cloud Messaging) |
| `FN_FCM_PROJECT_ID` | — | proyecto de Firebase; obligatoria con `fcm` |
| `FN_PUSH_TTL_S` | `86400` | cuánto vive el aviso en Firebase si el teléfono está apagado o sin red |
| `FN_PUSH_CONCURRENCIA` | `5` | envíos simultáneos por mensaje |
| `FN_INTENTOS_ENVIO` | `3` | intentos por mensaje antes de devolverlo a Pub/Sub |
| `FN_OTEL_ENABLED` | `false` | con `true` exporta trazas y logs; usa `OTEL_EXPORTER_OTLP_ENDPOINT` y `OTEL_EXPORTER_OTLP_HEADERS` |

## Pruebas

```bash
pytest --cov=app          # gate de cobertura: 80 %
ruff check . && ruff format --check .
```

## Adjuntos (BITS-101)

El evento puede traer `adjuntos`: referencias a archivos de un bucket privado (`FN_ADJUNTOS_BUCKET`),
por ejemplo el PDF de la póliza que genera `fn-documentos`. La función los lee del bucket y los
manda adjuntos (máximo 10 MiB en total, `FN_ADJUNTOS_MAX_BYTES`). Solo lee del bucket configurado: un
evento no puede pedirle archivos de cualquier otro. Su cuenta de servicio necesita
`roles/storage.objectViewer` sobre ese bucket.

## Avisos push (BITS-229)

Una segunda suscripción *push* de Pub/Sub (filtro `tipo = EnviarPush`) llama a `POST /push`. El
evento llega completo desde CoreTransaccional: hasta 5 `tokens` de dispositivos, `titulo`, `cuerpo`
y la `ruta` que abre la app (`seguro` + id de la póliza). La función manda un mensaje por token,
en paralelo, por la interfaz HTTP v1 de Firebase Cloud Messaging, con prioridad alta, vida de 24
horas y como etiqueta el `id` del evento (una reentrega de Pub/Sub reemplaza el aviso en vez de
duplicarlo).

| Respuesta de Firebase | Qué hace |
|---|---|
| `UNREGISTERED` | token muerto: se registra y se confirma; los demás dispositivos siguen |
| `INVALID_ARGUMENT`, `SENDER_ID_MISMATCH` | rechazo definitivo: se registra y se confirma |
| 429, 5xx, permisos pendientes | transitorio: se reintenta y, si se agotan los intentos, `503` para que Pub/Sub reentregue |

Se autentica con las credenciales de su cuenta de servicio (sin llaves), que necesita
`roles/firebasecloudmessaging.admin`. Nunca se escribe el token completo: solo sus últimos
caracteres.
