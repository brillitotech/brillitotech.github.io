"""
Brillitotech · Lead Capture Serverless Function
================================================

Vercel mapea este archivo a la ruta POST /api.
Runtime: Python 3.12 nativo, sin frameworks web (cero FastAPI / Flask / Django).
Stack edge: BaseHTTPRequestHandler de la stdlib → mínimo cold-start y ~0 MB de
dependencias de runtime más allá de google-generativeai y requests.

Flujo por invocación (todo en milisegundos de wall-clock):
  1. Parsear body del POST (application/x-www-form-urlencoded o JSON).
  2. Validar 6 campos obligatorios del formulario de diagnóstico.
  3. Llamar a Gemini 2.5 Flash con prompt de sistema → reporte Markdown.
  4. Renderizar el Markdown a HTML mínimo (sin librerías extra).
  5. Despachar el correo vía Resend → cliente + copia interna a la dueña.
  6. Responder 200/400/500 en JSON limpio.

Optimización de huella de carbono digital:
- Modelo Flash elegido por menor energía/token vs Pro.
- Sin frameworks pesados: solo stdlib + 2 librerías específicas.
- Respuestas pequeñas; sin streaming; sin logs ruidosos.
- Falla controlada: jamás propaga stacktraces al cliente (evita reintentos
  del navegador y, por tanto, invocaciones duplicadas de la función).
"""

import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler

import google.generativeai as genai
import markdown as _markdown_lib
import requests


# ---------------------------------------------------------------------------
# Configuración (se lee una sola vez por cold start, se cachea en el módulo)
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
OWNER_NOTIFICATION_EMAIL = os.environ.get("OWNER_NOTIFICATION_EMAIL", "")
EMAIL_FROM = os.environ.get(
    "EMAIL_FROM",
    "onboarding@resend.dev",
)

# Si está en true, las respuestas de error 5xx incluyen el detalle de la
# excepción interna (típicamente el body de respuesta de Gemini o Resend).
# Útil en staging; en producción debe quedarse en false para no filtrar
# información sensible (API keys parciales, payloads, etc.) al cliente.
DEBUG_ERRORS = os.environ.get("DEBUG_ERRORS", "").lower() in ("1", "true", "yes")

# Header secreto de bypass para diagnóstico ad-hoc sin reconfigurar
# variables de entorno en Vercel. Si la request trae "X-Debug: 1",
# el handler incluye el repr(exc) en la respuesta 5xx. No hace nada
# en éxito: solo en errores. Seguro de exponer porque no revela
# secretos por sí mismo — solo lo que el servidor iba a loguear.
DEBUG_HEADER_NAME = "X-Debug"
DEBUG_HEADER_VALUE = "1"

# Catálogo exacto de campos que exige el brief.
REQUIRED_FIELDS = (
    "nombre",
    "empresa",
    "email",          # mapeado desde el campo HTML "correo" (ver wire)
    "proceso_manual", # mapeado desde el campo HTML "proceso"
    "herramientas",   # mapeado desde el campo HTML "stack"
    "volumen_mensual",# mapeado desde el campo HTML "volumen"
)

# Mapa wire del HTML → nombres canónicos del brief.
# El HTML existente usa "correo", "proceso", "stack", "volumen"; los renombramos
# al cruzar la frontera del backend para no romper un sitio ya desplegado.
WIRE_TO_CANONICAL = {
    "correo": "email",
    "proceso": "proceso_manual",
    "stack": "herramientas",
    "volumen": "volumen_mensual",
}

# Prompt de sistema del brief, con f-string lazy (se aplica por request).
# One-shot: incluye un ejemplo de output para que Gemini respete el formato
# EXACTO. Sin el ejemplo, Gemini responde conversacionalmente ("¡Excelente!
# Como Arquitecto de Soluciones Cloud...") en vez del Markdown estricto.
#
# Reglas reforzadas en este commit:
# - "completar las 3 secciones": antes Gemini cortaba en la sección 1
#   porque el one-shot sugería un output corto.
# - "cifras como [estimación sin auditoría]": el brief del proyecto
#   prohíbe fabricar números de rendimiento. En vez de eliminar los
#   rangos, los marcamos honestamente para que el lead los entienda
#   como orientativos.
SYSTEM_PROMPT_TEMPLATE = """\
Eres un Arquitecto de Soluciones Cloud. Tu salida es EXCLUSIVAMENTE Markdown
técnico. NO incluyas saludos, NO incluyas introducciones narrativas, NO
incluyas "Como Arquitecto de Soluciones Cloud..." ni frases similares.
Arranca DIRECTAMENTE con el header "## 1. DIAGNÓSTICO FINANCIERO".

REGLA CRÍTICA: Tu respuesta DEBE incluir las TRES secciones completas
(1, 2 y 3) en orden. Después de la sección 1 continuá con la 2, y
después de la 2 con la 3. NO termines el reporte hasta completar las 3.

FORMATO OBLIGATORIO (respeta los headers literales):

## 1. DIAGNÓSTICO FINANCIERO
- Horas/mes desperdiciadas (estimación): <rango orientativo>
- Costo/mes estimado (orientativo): <USD en rango>
- Costo/año estimado (orientativo): <USD en rango>
- Riesgos del status quo: <1 línea>

IMPORTANTE: Todas las cifras de esta sección son ESTIMACIONES
ORIENTATIVAS sin auditoría. Marcá cada cifra con el sufijo
"[estimación sin auditoría]" para que el lead entienda que requiere
validación con datos reales.

## 2. ARQUITECTURA DE LA SOLUCIÓN
<2-3 párrafos breves explicando el enfoque>

```mermaid
flowchart LR
  A[Origen del dato] --> B[Webhook o Agente ligero]
  B --> C[Procesamiento asíncrono]
  C --> D[Destino / Notificación]
```

## 3. STACK RECOMENDADO
- <herramienta 1>: <justificación de 1 línea>
- <herramienta 2>: <justificación de 1 línea>
- <herramienta 3>: <justificación de 1 línea>
- <herramienta 4>: <justificación de 1 línea>

Sé crítico. Si el proceso no se justifica automatizar con IA, dilo
claramente en DIAGNÓSTICO y propón una optimización de base.

---
Datos del cliente:
- Empresa: {empresa}
- Proceso crítico manual: {proceso_manual}
- Stack actual: {herramientas}
- Volumen mensual: {volumen_mensual}"""


# ---------------------------------------------------------------------------
# Helpers de parseo y validación
# ---------------------------------------------------------------------------

def parse_request_body(handler: BaseHTTPRequestHandler) -> dict:
    """
    Lee el body del POST soportando los dos content-types que un formulario
    HTML sin JS puede emitir:
      - application/x-www-form-urlencoded (default del <form>)
      - application/json (clientes JS / fetch)

    Devuelve SIEMPRE un dict plano con strings.
    """
    content_length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(content_length) if content_length > 0 else b""
    content_type = (handler.headers.get("Content-Type") or "").lower()

    if "application/json" in content_type:
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
            return {k: ("" if v is None else str(v)) for k, v in data.items()}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # Default: form-urlencoded.
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    parsed = urllib.parse.parse_qs(decoded, keep_blank_values=True)
    # parse_qs devuelve listas; aplanamos al primer valor.
    return {k: (v[0] if v else "") for k, v in parsed.items()}


def normalize_wire_fields(raw: dict) -> dict:
    """
    Aplica el mapeo wire→canónico. Si el cliente ya envía el nombre canónico
    (por ejemplo desde fetch con JSON), también funciona.
    """
    normalized = dict(raw)
    for wire_name, canonical in WIRE_TO_CANONICAL.items():
        if wire_name in normalized and canonical not in normalized:
            normalized[canonical] = normalized[wire_name]
    return normalized


def is_valid_email(value: str) -> bool:
    """Validación pragmática: basta con presencia de @ y un punto en el dominio."""
    if not value or len(value) > 254:
        return False
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value))


def validate_payload(payload: dict) -> tuple[bool, str]:
    """
    Verifica presencia de los 6 campos obligatorios y formato del email.
    Devuelve (ok, mensaje_de_error). El mensaje es seguro para devolver al cliente.
    """
    missing = [f for f in REQUIRED_FIELDS if not (payload.get(f) or "").strip()]
    if missing:
        return False, f"Campos requeridos faltantes: {', '.join(missing)}"

    if not is_valid_email(payload["email"]):
        return False, "El campo email no tiene un formato válido."

    return True, ""


# ---------------------------------------------------------------------------
# Integración Gemini (Google AI Studio)
# ---------------------------------------------------------------------------

def generate_blueprint(payload: dict) -> str:
    """
    Llama a Gemini 2.5 Flash y devuelve el reporte en Markdown.
    El SDK se inicializa lazy para que el cold-start no pague el costo de
    configuración si la request falla antes por validación.
    """
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash")

    prompt = SYSTEM_PROMPT_TEMPLATE.format(
        nombre=payload["nombre"].strip(),
        empresa=payload["empresa"].strip(),
        proceso_manual=payload["proceso_manual"].strip(),
        herramientas=payload["herramientas"].strip(),
        volumen_mensual=payload["volumen_mensual"].strip(),
    )

    # Configuración deliberadamente económica: temperature baja para
    # análisis técnico consistente, max_output_tokens acotado para minimizar
    # el cómputo total por invocación.
    response = model.generate_content(
        prompt,
        generation_config=genai.types.GenerationConfig(
            temperature=0.15,   # bajado a 0.15: más determinístico, mejor
                                # adherencia al formato completo de 3
                                # secciones (antes cortaba en sección 1)
            max_output_tokens=3500,   # subido de 2048: las 3 secciones +
                                       # el bloque Mermaid + el stack no
                                       # entraban en 2048 tokens
        ),
    )
    return (response.text or "").strip()


# ---------------------------------------------------------------------------
# Render Markdown → HTML
# ---------------------------------------------------------------------------
# Migrado de regex propio a la librería `markdown` (referencia en
# CommonMark + GFM). El render regex casero fallaba en listas anidadas,
# escapaba mal el contenido de los bloques de código, y rompía headers
# cuando el texto tenía caracteres especiales. La dependencia es ~100KB
# en disco pero en runtime el cold-start no la paga porque Vercel cachea
# el wheel de Python entre invocaciones del mismo runtime.
#
# Bloques ```mermaid```: el cliente de correo NO renderiza Mermaid, así
# que los entregamos como <pre class="mermaid"> con el código raw
# (legible como texto plano). El lead puede pegar el código en
# https://mermaid.live para visualizarlo.

_MD_RENDERER = _markdown_lib.Markdown(
    extensions=[
        "fenced_code",   # bloques ```lang```
        "tables",        # tablas GFM
        "sane_lists",    # listas que no dependen de un offset de 4 espacios exacto
        "nl2br",         # saltos de línea suaves → <br>
    ],
    output_format="html",
)


def markdown_to_html(md: str) -> str:
    """Convierte Markdown a HTML usando python-markdown."""
    if not md:
        return ""
    # El renderer mantiene estado interno entre llamadas (reset es obligatorio).
    _MD_RENDERER.reset()
    return _MD_RENDERER.convert(md)


# ---------------------------------------------------------------------------
# Integración Resend
# ---------------------------------------------------------------------------

def send_email(to_email: str, subject: str, markdown_body: str, html_body: str) -> None:
    """
    Despacha el correo vía Resend. Lanza requests.HTTPError si falla.
    Resend se llama UNA sola vez por request: el cliente recibe el informe
    y la dueña del portafolio queda en copia oculta (BCC-like, vía segundo
    destinatario explícito) para lead-notification.
    """
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY no configurada en el entorno.")

    to_list = [to_email]
    # BCC para el owner (no destinatario visible). El lead no ve el email
    # del owner en la cabecera "Para:", sigue siendo una sola request a
    # Resend. Validamos que el email del owner sea parseable para no
    # romper toda la request con un 422.
    bcc_list = None
    if (
        OWNER_NOTIFICATION_EMAIL
        and OWNER_NOTIFICATION_EMAIL.strip()
        and "@" in OWNER_NOTIFICATION_EMAIL
        and OWNER_NOTIFICATION_EMAIL.lower() != to_email.lower()
    ):
        bcc_list = [OWNER_NOTIFICATION_EMAIL.strip()]

    payload = {
        "from": EMAIL_FROM,
        "to": to_list,
        "subject": subject,
        "text": markdown_body,
        "html": html_body,
    }
    if bcc_list:
        payload["bcc"] = bcc_list

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=10,
    )
    if not response.ok:
        # Capturamos el body del error ANTES de raise_for_status para que
        # quede en el log de Vercel. Resend devuelve JSON con {"message": "..."}
        # que suele ser mucho más diagnóstico que el código de status.
        try:
            err_body = response.json()
        except ValueError:
            err_body = {"raw": response.text[:500]}
        # Adjuntamos el detalle al raise para que el caller lo loguee completo.
        raise requests.HTTPError(
            f"Resend {response.status_code}: {err_body}",
            response=response,
        )
    response.raise_for_status()


# ---------------------------------------------------------------------------
# Helpers de respuesta HTTP
# ---------------------------------------------------------------------------

# URL a la que redirigir tras un POST exitoso desde el formulario HTML.
# En éxito, SIEMPRE se prefiere redirect (302) sobre JSON para que el navegador
# nativo siga el flujo sin requerir JavaScript. Si el cliente pidió
# explícitamente JSON (Accept: application/json o header X-Requested-With),
# se devuelve 200 con cuerpo JSON para integraciones programáticas.
SUCCESS_REDIRECT_URL = os.environ.get(
    "SUCCESS_REDIRECT_URL",
    "https://services-ia.vercel.app/gracias.html",
)


def wants_json_response(handler: BaseHTTPRequestHandler) -> bool:
    """Detecta si el cliente espera JSON en vez de un redirect HTML."""
    accept = (handler.headers.get("Accept") or "").lower()
    if "application/json" in accept:
        return True
    if handler.headers.get("X-Requested-With", "").lower() == "fetch":
        return True
    return False


def is_debug_request(handler: BaseHTTPRequestHandler) -> bool:
    """
    Devuelve True si el handler debe incluir detalles internos en la
    respuesta de error. Se activa si:
      - DEBUG_ERRORS=true en el entorno (Vercel env var), o
      - la request trae el header X-Debug: 1 (diagnóstico ad-hoc con curl).
    """
    if DEBUG_ERRORS:
        return True
    if handler.headers.get(DEBUG_HEADER_NAME, "").strip() == DEBUG_HEADER_VALUE:
        return True
    return False


def error_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    public_message: str,
    exc: BaseException | None = None,
) -> None:
    """Escribe una respuesta 5xx uniforme, con campo debug opcional."""
    body: dict = {"status": "error", "message": public_message}
    if exc is not None and is_debug_request(handler):
        body["debug"] = repr(exc)
    write_json(handler, status, body)


def write_json(handler: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(encoded)


def write_redirect(handler: BaseHTTPRequestHandler, location: str) -> None:
    """
    Emite un 302 Found con header Location. Usado en la rama de éxito del
    formulario HTML nativo: el navegador sigue el redirect sin necesidad
    de JavaScript en el cliente.
    """
    handler.send_response(302)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()


# ---------------------------------------------------------------------------
# Handler principal (Vercel entrypoint)
# ---------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    """Vercel detecta la clase `handler` y enruta POST /api → do_POST."""

    # Silencia los logs de acceso por defecto de BaseHTTPRequestHandler
    # para no inflar el output del runtime serverless.
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def do_POST(self) -> None:  # noqa: N802 (nombre exigido por la stdlib)
        try:
            # 1) Parseo + normalización wire→canónico.
            raw = parse_request_body(self)
            payload = normalize_wire_fields(raw)

            # 2) Validación.
            ok, err = validate_payload(payload)
            if not ok:
                write_json(self, 400, {"status": "error", "message": err})
                return

            # 3) Generación del plano técnico con Gemini.
            try:
                markdown_body = generate_blueprint(payload)
            except Exception as exc:  # noqa: BLE001
                # Log interno sin filtrar al cliente.
                print(f"[gemini] error: {exc!r}")
                error_response(
                    self,
                    500,
                    "No fue posible generar el plano técnico. Intenta de nuevo en unos minutos.",
                    exc=exc,
                )
                return

            if not markdown_body:
                error_response(
                    self,
                    500,
                    "El modelo no devolvió contenido.",
                )
                return

            # 4) Render Markdown → HTML.
            html_body = markdown_to_html(markdown_body)

            # 5) Despacho vía Resend.
            try:
                subject = f"Tu Plano de Ingeniería · {payload['empresa'].strip()}"
                send_email(
                    to_email=payload["email"].strip(),
                    subject=subject,
                    markdown_body=markdown_body,
                    html_body=html_body,
                )
            except Exception as exc:  # noqa: BLE001
                # Logueo COMPLETO en Vercel: status + body de Resend si está
                # disponible. Esto es lo que te dice si el problema es el
                # 'from' no verificado, API key mala, rate limit, etc.
                print(f"[resend] error: {exc!r}")
                error_response(
                    self,
                    500,
                    "Generamos tu plano pero falló el envío del correo. Escríbenos y te lo reenviamos.",
                    exc=exc,
                )
                return

            # 6) Éxito. Default: redirect 302 a la página estática de gracias
            #    (compatible con el flujo del <form> sin JavaScript). Si el
            #    cliente pidió JSON explícitamente, devolvemos 200 + JSON
            #    para integraciones programáticas (fetch, curl, tests).
            if wants_json_response(self):
                write_json(
                    self,
                    200,
                    {"status": "success", "message": "Plano técnico enviado con éxito."},
                )
            else:
                write_redirect(self, SUCCESS_REDIRECT_URL)

        except Exception as exc:  # noqa: BLE001
            # Última barrera: jamás propagues stacktrace al cliente.
            print(f"[unhandled] {exc!r}")
            write_json(
                self,
                500,
                {"status": "error", "message": "Error inesperado procesando la solicitud."},
            )
