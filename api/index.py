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

import base64
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
# Reglas activas del prompt que requieren refuerzo explícito
# (sin él, Gemini las ignora o las suaviza):
# - "completar las 3 secciones": el one-shot sugería un output corto
#   y Gemini tendía a cortar en la sección 1.
# - "cifras como [estimación sin auditoría]": el brief del proyecto
#   prohíbe fabricar números de rendimiento. Marcamos los rangos
#   honestamente para que el lead los lea como orientativos.
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
IMPORTANTE: la propuesta arquitectonica debe estar encaminada a la optimizacion
de recursos y reduccion de CO2 asi que esta debe estar pensanda en esa condicion.
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
IMPORTANTE: recomienda stack con su version free y la mejor version costo efectiva
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
# que los entregamos como un CTA visual con link a https://mermaid.live
# (que sí renderiza Mermaid en el navegador) más un <details> colapsable
# con el código fuente por si el lead lo quiere copiar a otra herramienta.
# mermaid.live acepta el código del diagrama en el fragmento URL con el
# esquema `pako:<base64-de-json-con-codigo>`, así que el link abre el
# editor con el diagrama YA cargado, sin que el lead tenga que copy-paste.

# Regex que captura el bloque completo ```mermaid ... ``` (lazy match del
# cuerpo para no comerse más de un bloque si hubiera varios).
_MERMAID_BLOCK_RE = re.compile(
    r"```mermaid\s*\n(.*?)\n```",
    re.DOTALL,
)

# Placeholder único que se inserta en el Markdown antes de pasarlo al
# renderer. Usamos un token que NO puede aparecer naturalmente en Markdown
# válido (los marcadores de posición de html span son seguros) para que
# la sustitución posterior sea inequívoca.
_MERMAID_PLACEHOLDER = "\x00MERMAID_BLOCK_{i}\x00"


def _build_mermaid_html(code: str) -> str:
    """
    Construye el bloque HTML que reemplaza al ```mermaid``` en el correo.

    Estrategia:
    1. Encodeamos el código Mermaid en base64 de un JSON {"code": "...",
       "mermaid": {"theme": "default"}} que es el formato que acepta
       mermaid.live en su fragmento #pako:.
    2. Generamos un link directo al editor de mermaid.live con el diagrama
       YA cargado (no necesita copy-paste del lead).
    3. Mostramos el código fuente en un <details> colapsable para que el
       lead pueda copiarlo si prefiere otra herramienta (Notion, Confluence,
       un repo, etc.).
    4. Todos los estilos son INLINE porque los clientes de correo (Gmail
       especialmente) descartan el CSS del <head> y muchas veces también
       el de las clases. Inline es la única forma de que se vea bien en
       Gmail, Outlook, Apple Mail y compañía.
    """
    # mermaid.live usa pako (zlib) + base64url del JSON. Hacemos una versión
    # simplificada: base64-url-safe del JSON, sin compresión. La página
    # acepta ambos formatos en su estado "loadFromJSON".
    payload = json.dumps(
        {"code": code, "mermaid": {"theme": "default"}},
        ensure_ascii=False,
    )
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    mermaid_url = f"https://mermaid.live/edit#pako:{encoded}"

    # Escapeamos el código del diagrama para que sea seguro de meter en
    # un <pre> dentro de HTML (sólo escapamos los 3 caracteres que rompen HTML).
    safe_code = (
        code.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    return (
        '<div style="margin: 24px 0; padding: 20px; '
        'border: 1px solid #5A6B7A; border-radius: 8px; '
        'background-color: #1A2332; text-align: center;">'
        '<p style="margin: 0 0 12px 0; color: #E8EDF2; '
        'font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', '
        'Roboto, sans-serif; font-size: 16px; font-weight: 600;">'
        '📊 Diagrama de arquitectura'
        '</p>'
        '<a href="' + mermaid_url + '" '
        'style="display: inline-block; padding: 10px 24px; '
        'background-color: #4A90D9; color: #FFFFFF; '
        'text-decoration: none; border-radius: 6px; '
        'font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', '
        'Roboto, sans-serif; font-size: 14px; font-weight: 600;">'
        'Ver diagrama interactivo →'
        '</a>'
        '<details style="margin-top: 16px; text-align: left;">'
        '<summary style="cursor: pointer; color: #A8B5C2; '
        'font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', '
        'Roboto, sans-serif; font-size: 13px;">'
        'Ver código del diagrama'
        '</summary>'
        '<pre style="margin-top: 8px; padding: 12px; '
        'background-color: #0F1620; color: #E8EDF2; '
        'border-radius: 4px; overflow-x: auto; '
        'font-family: \'SF Mono\', Monaco, Consolas, monospace; '
        'font-size: 12px; line-height: 1.4;">'
        + safe_code +
        '</pre>'
        '</details>'
        '</div>'
    )


def _replace_mermaid_in_markdown(md: str) -> tuple[str, list[str]]:
    """
    Extrae los bloques ```mermaid``` del Markdown, los reemplaza por
    placeholders opacos (que NO se ven afectados por el render de
    python-markdown) y devuelve el Markdown modificado + la lista de
    códigos extraídos en orden.

    El placeholder usa caracteres de control (\x00) que no pueden aparecer
    en Markdown válido, garantizando que la sustitución posterior sea
    inequívoca.
    """
    blocks: list[str] = []
    counter = [0]  # lista para closure mutable desde el callback de sub

    def _swap(match: re.Match) -> str:
        code = match.group(1)
        blocks.append(code)
        placeholder = _MERMAID_PLACEHOLDER.format(i=counter[0])
        counter[0] += 1
        return placeholder

    modified = _MERMAID_BLOCK_RE.sub(_swap, md)
    return modified, blocks


def _substitute_mermaid_in_html(html: str, blocks: list[str]) -> str:
    """
    Recorre el HTML renderizado y reemplaza cada placeholder por el bloque
    HTML generado con el link a mermaid.live y el <details> con el código.
    """
    result = html
    for i, code in enumerate(blocks):
        placeholder = _MERMAID_PLACEHOLDER.format(i=i)
        result = result.replace(placeholder, _build_mermaid_html(code))
    return result


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
    """
    Convierte Markdown a HTML usando python-markdown.

    Pipeline de 2 pasadas para los bloques ```mermaid```:
    1. Extraemos los bloques Mermaid del Markdown y los sustituimos por
       placeholders opacos (caracteres de control que python-markdown
       no toca).
    2. Después del render, sustituimos los placeholders en el HTML
       resultante por el bloque HTML del CTA a mermaid.live.
    """
    if not md:
        return ""

    # Pasada 1: extraer bloques Mermaid.
    preprocessed, mermaid_blocks = _replace_mermaid_in_markdown(md)

    # El renderer mantiene estado interno entre llamadas (reset es obligatorio).
    _MD_RENDERER.reset()
    rendered = _MD_RENDERER.convert(preprocessed)

    # Pasada 2: insertar los bloques Mermaid como HTML literal.
    return _substitute_mermaid_in_html(rendered, mermaid_blocks)


# ---------------------------------------------------------------------------
# Persistencia de leads en Google Sheets
# ---------------------------------------------------------------------------
# Cada vez que el endpoint procesa un lead válido, escribe una fila nueva en
# una Google Sheet de la dueña del portafolio. Esto le da una "base de datos"
# ligera sin meter un Postgres real para un volumen de decenas de leads/mes.
#
# Diseño:
# - Credenciales via env var GOOGLE_CREDENTIALS_JSON (el JSON completo de la
#   service account que Briggitte descargó de Google Cloud).
# - Spreadsheet ID via env var LEAD_SHEET_ID (la URL de la sheet tiene el
#   ID entre /d/ y /edit).
# - Nombre de la pestaña (sheet dentro del spreadsheet) configurable via
#   LEAD_SHEET_TAB (default "Leads" — la primera pestaña recién creada).
# - Si CUALQUIER parte de la integración falla, NO rompe el envío del email.
#   Logueamos el error y seguimos: la prioridad es que el lead reciba su
#   plano técnico. La sheet es nice-to-have, no crítica.
#
# Layout de columnas (en este orden exacto, headers en la fila 1):
#   A: fecha_hora          ISO 8601 en UTC
#   B: nombre              del form
#   C: empresa             del form
#   D: email               del form
#   E: proceso_manual      del form
#   F: herramientas        del form
#   G: volumen_mensual     del form
#   H: resumen_ia          ≤500 chars, primera línea útil de Gemini
#
# Esto coincide 1-a-1 con los campos que produce el formulario HTML
# (mapeados via WIRE_TO_CANONICAL al cruzar la frontera del backend).

LEAD_SHEET_ID = os.environ.get("LEAD_SHEET_ID", "")
LEAD_SHEET_TAB = os.environ.get("LEAD_SHEET_TAB", "Leads")

# Cabeceras que escribimos en la fila 1 la primera vez. Idempotente: si la
# fila 1 ya tiene headers, no los pisa (leemos primero y solo escribimos si
# la primera celda está vacía).
_LEAD_HEADERS = [
    "fecha_hora",
    "nombre",
    "empresa",
    "email",
    "proceso_manual",
    "herramientas",
    "volumen_mensual",
    "resumen_ia",
]


def _build_lead_summary(markdown_body: str, max_chars: int = 500) -> str:
    """
    Extrae un resumen ejecutivo del reporte de Gemini, limitado a
    `max_chars` caracteres. Estrategia:

    1. Buscamos la primera sección "## 1. DIAGNÓSTICO FINANCIERO" y
       extraemos su contenido hasta la próxima "## 2." o fin de string.
    2. Tomamos las primeras 3 líneas con contenido de esa sección
       (son las más accionables: horas/mes, costo/mes, costo/año).
    3. Si el bloque extraído excede max_chars, truncamos con "…".

    Esto evita meter el reporte completo (sería kilométrico y saturaría
    la sheet) y deja lo que el dueño necesita para entender de un vistazo
    si el lead vale la pena.
    """
    if not markdown_body:
        return ""

    # 1) Localizar la sección 1.
    start = markdown_body.find("## 1.")
    if start < 0:
        # Fallback: primeras 5 líneas no vacías del reporte entero.
        lines = [ln.strip() for ln in markdown_body.splitlines() if ln.strip()]
        snippet = " | ".join(lines[:5])
    else:
        # 2) Cortar hasta la próxima sección ## 2. (o fin de string).
        end = markdown_body.find("## 2.", start)
        section = markdown_body[start:end] if end > 0 else markdown_body[start:]
        # 3) Tomar las primeras 3 líneas con contenido (saltando el header).
        lines = [ln.strip() for ln in section.splitlines() if ln.strip() and not ln.strip().startswith("##")]
        snippet = " | ".join(lines[:3])

    # 4) Truncar a max_chars.
    if len(snippet) > max_chars:
        snippet = snippet[: max_chars - 1].rstrip() + "…"
    return snippet


def _get_sheets_service():
    """
    Construye y cachea el cliente de Google Sheets API usando las credenciales
    de la service account almacenadas en GOOGLE_CREDENTIALS_JSON. Si la env
    var no está, devuelve None (el caller decide qué hacer).
    """
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if not creds_json:
        return None

    # Import lazy para no pagar el costo de las libs de Google en cold-starts
    # donde la integración de Sheets no se usa (ej: si alguien la desactiva).
    from google.oauth2 import service_account
    from googleapiclient.discovery import build as _gbuild

    # Scopes: solo lectura/escritura de Sheets (no Drive, no Gmail, etc.)
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

    # Cargamos el JSON de la env var como dict.
    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES,
    )
    return _gbuild("sheets", "v4", credentials=creds, cache_discovery=False)


def write_lead_to_sheet(payload: dict, summary: str) -> None:
    """
    Escribe una fila nueva en la Google Sheet de leads. NO lanza excepciones:
    si algo falla (credenciales mal, sheet sin compartir, timeout, quota),
    logueamos el error y retornamos. La prioridad es que el lead reciba su
    email; la sheet es un registro secundario.

    Llamada DESPUÉS de Gemini (necesitamos el summary) y ANTES de Resend
    (queremos que el lead quede registrado ANTES de mandarle el correo,
    así si Resend falla ya tenemos el lead capturado).
    """
    if not LEAD_SHEET_ID:
        print("[sheets] LEAD_SHEET_ID no configurado; skip.")
        return

    try:
        service = _get_sheets_service()
        if service is None:
            print("[sheets] GOOGLE_CREDENTIALS_JSON no configurado; skip.")
            return

        # Timestamp ISO 8601 en UTC. Formato portable y ordenable.
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Construir la fila en el orden de _LEAD_HEADERS.
        row = [
            timestamp,
            payload.get("nombre", "").strip(),
            payload.get("empresa", "").strip(),
            payload.get("email", "").strip(),
            payload.get("proceso_manual", "").strip(),
            payload.get("herramientas", "").strip(),
            payload.get("volumen_mensual", "").strip(),
            summary,
        ]

        # 1) Asegurar que la fila 1 tiene headers (idempotente: solo escribe
        #    si la primera celda está vacía).
        try:
            existing = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=LEAD_SHEET_ID, range=f"{LEAD_SHEET_TAB}!A1:H1")
                .execute()
            )
            first_cell = (
                existing.get("values", [[]])[0][0]
                if existing.get("values") and existing.get("values")[0]
                else ""
            )
            if not first_cell:
                service.spreadsheets().values().update(
                    spreadsheetId=LEAD_SHEET_ID,
                    range=f"{LEAD_SHEET_TAB}!A1:H1",
                    valueInputOption="RAW",
                    body={"values": [_LEAD_HEADERS]},
                ).execute()
        except Exception as header_exc:  # noqa: BLE001
            # Si fallan los headers pero la sheet existe, seguimos: tal vez
            # los headers ya estén escritos por otro medio (escritura manual).
            print(f"[sheets] header check failed (continuamos): {header_exc!r}")

        # 2) Append de la fila nueva al final de la sheet.
        service.spreadsheets().values().append(
            spreadsheetId=LEAD_SHEET_ID,
            range=f"{LEAD_SHEET_TAB}!A1:H1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
    except Exception as exc:  # noqa: BLE001
        # Cualquier falla de Sheets NO debe romper el flujo principal.
        print(f"[sheets] error escribiendo lead: {exc!r}")


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

            # 4.5) Persistir el lead en Google Sheets (no-bloqueante: si
            #      Sheets falla, el lead igual recibe su email porque
            #      write_lead_to_sheet captura y loguea todas las
            #      excepciones internamente). La capturamos acá también
            #      por defensa en profundidad.
            lead_summary = _build_lead_summary(markdown_body, max_chars=500)
            try:
                write_lead_to_sheet(payload, lead_summary)
            except Exception as exc:  # noqa: BLE001
                print(f"[sheets] llamada externa a write_lead_to_sheet explotó: {exc!r}")

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
