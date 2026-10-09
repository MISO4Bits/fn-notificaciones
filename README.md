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
| `FN_INTENTOS_ENVIO` | `3` | intentos por mensaje antes de devolverlo a Pub/Sub |
| `FN_OTEL_ENABLED` | `false` | con `true` exporta trazas y logs; usa `OTEL_EXPORTER_OTLP_ENDPOINT` y `OTEL_EXPORTER_OTLP_HEADERS` |

## Pruebas

```bash
pytest --cov=app          # gate de cobertura: 80 %
ruff check . && ruff format --check .
```
