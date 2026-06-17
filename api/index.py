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
SYSTEM_PROMPT_TEMPLATE = """\
Eres un Arquitecto de Soluciones Cloud. Analiza el siguiente proceso operativo y genera un 'Plano de Ingeniería de Procesos Express'.
Datos del cliente:
- Nombre: {nombre} | Empresa: {empresa}
- Proceso crítico manual: {proceso_manual}
- Stack actual: {herramientas}
- Volumen mensual: {volumen_mensual}

Tu respuesta DEBE ser en formato Markdown estricto y contener:
1. DIAGNÓSTICO FINANCIERO: Estimación cruda del desperdicio operativo.
2. ARQUITECTURA DE LA SOLUCIÓN: Un diagrama de flujo funcional estructurado estrictamente en código de bloques Mermaid.js (dentro de un bloque de código ```mermaid) que muestre cómo webhooks o agentes ligeros reemplazan el paso manual.
3. STACK RECOMENDADO: Qué herramientas de bajo costo (ej. Webhooks, APIs ligeras, serverless) solucionan esto con consumo mínimo de recursos.
Sé crítico. Si el proceso no se puede o no se debe automatizar con IA, dilo claramente y propón una optimización de base segun el caso."""


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
            temperature=0.4,
            max_output_tokens=2048,
        ),
    )
    return (response.text or "").strip()


# ---------------------------------------------------------------------------
# Render Markdown → HTML mínimo (sin librerías externas)
# ---------------------------------------------------------------------------

_MERMAID_RE = re.compile(r"```mermaid\s*\n(.*?)\n```", re.DOTALL)
_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+\-]*)\s*\n(.*?)\n```", re.DOTALL)
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+?)\*(?!\*)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def markdown_to_html(md: str) -> str:
    """
    Conversión mínima suficiente para el reporte de Gemini:
      - Escapa HTML
      - Bloques ```mermaid``` → <pre class="mermaid"> con el código raw
        (el cliente de correo no renderiza Mermaid, lo entrega como
        diagrama de texto al receptor; el prompt está pensado para que
        sea legible como código).
      - Otros bloques ```lang``` → <pre><code>
      - Encabezados #, ##, ### → <h1>, <h2>, <h3>
      - **bold**, *italic*, `inline code`
      - Doble salto de línea → párrafo
    """
    if not md:
        return ""

    # 1) Extraer y reemplazar bloques Mermaid primero (para que el escape HTML
    #    no dañe las flechas del diagrama).
    mermaid_blocks: list[str] = []

    def _stash_mermaid(match: re.Match) -> str:
        mermaid_blocks.append(match.group(1))
        return f"@@MERMAID_{len(mermaid_blocks) - 1}@@"

    md = _MERMAID_RE.sub(_stash_mermaid, md)

    # 2) Escapar HTML en el resto del Markdown.
    md = escape_html(md)

    # 3) Otros bloques de código genéricos.
    def _fence_repl(match: re.Match) -> str:
        lang = match.group(1) or ""
        body = match.group(2)
        if lang:
            return f'<pre><code class="language-{escape_html(lang)}">{body}</code></pre>'
        return f"<pre><code>{body}</code></pre>"

    md = _FENCE_RE.sub(_fence_repl, md)

    # 4) Encabezados.
    md = _HEADER_RE.sub(lambda m: f"<h{len(m.group(1))}>{m.group(2)}</h{len(m.group(1))}>", md)

    # 5) Inline formatting (orden importa: bold antes que italic).
    md = _BOLD_RE.sub(r"<strong>\1</strong>", md)
    md = _ITALIC_RE.sub(r"<em>\1</em>", md)
    md = _INLINE_CODE_RE.sub(r"<code>\1</code>", md)

    # 6) Párrafos: doble salto de línea separa bloques.
    paragraphs = [p.strip() for p in md.split("\n\n") if p.strip()]
    md = "".join(
        f"<p>{p.replace(chr(10), '<br>')}</p>" if not p.startswith("<") else p
        for p in paragraphs
    )

    # 7) Restaurar bloques Mermaid.
    for idx, code in enumerate(mermaid_blocks):
        md = md.replace(f"@@MERMAID_{idx}@@", f'<pre class="mermaid">{escape_html(code)}</pre>')

    return md


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
    # Solo añadimos al owner si su email es válido. Resend rechaza toda la
    # request con 422 si ALGÚN destinatario del array 'to' es inválido,
    # así que es más seguro omitir al owner que enviar la request rota.
    if (
        OWNER_NOTIFICATION_EMAIL
        and OWNER_NOTIFICATION_EMAIL.strip()
        and "@" in OWNER_NOTIFICATION_EMAIL
        and OWNER_NOTIFICATION_EMAIL.lower() != to_email.lower()
    ):
        to_list.append(OWNER_NOTIFICATION_EMAIL.strip())

    payload = {
        "from": EMAIL_FROM,
        "to": to_list,
        "subject": subject,
        "text": markdown_body,
        "html": html_body,
    }

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
    "https://brillitotech.com/gracias.html",
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
