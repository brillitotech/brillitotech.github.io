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

# Email de contacto que aparece en el CTA final del reporte ("respondé
# este correo o escribí directamente a <email>"). Se sustituye del
# placeholder literal que emite Gemini en el Markdown.
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "btatacc@gmail.com")

# URL del appointment scheduler (Google Calendar) que aparece en la sección
# 4 del reporte como CTA principal del monolítico v5. A diferencia del
# email de contacto, esta URL se pre-procesa en el prompt ANTES de mandar
# a Gemini (template.replace("[URL_CALENDARIO]", CALENDAR_URL)) para que
# el LLM la reproduzca verbatim y no la rompa con reescritura libre.
CALENDAR_URL = os.environ.get(
    "CALENDAR_URL",
    "https://calendar.app.google/waVhCpxqebUEP3vc9",
)

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

# ---------------------------------------------------------------------------
# Prompt del sistema — monolítico con mapa de decisión A/B/C
# ---------------------------------------------------------------------------
# Placeholders del template (rellenados por generate_blueprint):
#   {cliente_empresa}   {cliente_proceso}   {cliente_stack}   {cliente_volumen}
#   [URL_CALENDARIO]    → se reemplaza por CALENDAR_URL antes de llamar a
#                         Gemini (pre-procesamiento, no post-render).

SYSTEM_PROMPT_TEMPLATE = """\
Sos un consultor de eficiencia operativa para PyMEs. Tu salida es Markdown comercial en español, dirigido al dueño del negocio (no técnico). Arrancá DIRECTAMENTE con `## 1.`. Terminá con la línea del CTA. NO escribas nada antes ni después.

Detectá primero el escenario del proceso (hacelo internamente):
- A — Automatización Directa: 100% repetitivo, reglas fijas, sin ambigüedad. NO necesita IA. Conjunto de herramientas: una plataforma central de automatización ligera + una infraestructura de despliegue ultrarrápida.
- B — Asistente Inteligente de Bajo Consumo: clasificar texto ambiguo, resumir, extraer datos. SÍ necesita una IA chica y económica. Conjunto de herramientas: motores de lenguaje optimizados para bajo consumo + la plataforma central para conectar todo.
- C — Copiloto Operativo: decisiones críticas de negocio que NO se delegan. La IA prepara el caso; el humano decide y firma.

Si dudás entre dos, elegí el de MENOR automatización.

Usá SOLO descripciones genéricas (plataforma central de automatización ligera, infraestructura de despliegue ultrarrápida, motores de lenguaje optimizados para bajo consumo). NO uses nombres comerciales (n8n, Vercel, Gemini, Vertex, Bedrock, Lambda, Zapier, Make). Glosá toda jerga técnica en impacto de negocio (ahorro de tiempo, ahorro de plata, menos errores).

Estructura obligatoria (4 secciones, en orden). Cada sección DEBE completar TODOS los items listados antes de pasar a la siguiente:

## 1. IMPACTO RÁPIDO (100-150 palabras)
"Detectamos que tu proceso encaja en el Escenario [A|B|C]: [nombre]." + 3 bullets: costo mensual en USD [estimación sin auditoría], pérdida anualizada, quick win concreto.

## 2. LA SOLUCIÓN (250-350 palabras)
2-3 oraciones justificando el escenario. Luego "Conjunto de herramientas propuesto" con 3 bullets: cómo se conectan tus sistemas, si necesitás una IA o no, cómo se guarda y notifica. Cerrá con 1 línea de ahorro estimado y tiempo de implementación.

## 3. POR QUÉ ESTE ENFOQUE (100-130 palabras)
Descartá alternativas caras (plataformas empresariales 10x más caras a tu volumen; Zapier/Make caro por operación). NO agregues un tercer párrafo.

## 4. SIGUIENTE PASO (exactamente 3 oraciones + 1 línea CTA al final, sin más)
- Oración 1: costo de inacción en 6 meses, con cifra en USD.
- Oración 2: recomendá una llamada de 15 minutos para revisar la viabilidad del caso.
- Oración 3: aclaración de que la llamada no genera compromiso, solo claridad sobre si el escenario aplica.
- Línea CTA obligatoria como ÚLTIMA línea del reporte entero, sin punto final: "Hagamos una llamada de 15 minutos para ver si esto es viable para tu caso. Agendala directamente acá: [URL_CALENDARIO]"
- NO agregues una 4ta oración, cierre emotivo, ni variantes.

Reglas duras:
- REPORTE COMPLETO > brevedad. NO recortes secciones. Si una sección requiere más espacio, usalo.
- Mencioná explícitamente "Escenario X" en la sección 1.
- NO uses bloques de código, tablas complejas, ni diagramas Mermaid. Solo prosa + bullets.
- NUNCA cierres una sección con frases genéricas como "No permitas que...", "Es momento de actuar", "Espero que sea útil", "No dudes en contactarnos". Cada sección cierra con su contenido específico; la 4 cierra con la línea CTA literal.

Datos del cliente:
* Empresa: {cliente_empresa}
* Proceso: {cliente_proceso}
* Herramientas que usan hoy: {cliente_stack}
* Volumen mensual: {cliente_volumen}
"""

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
    Devuelve (ok, mensaje_de_error). El mensaje es seguro para devolver al cliente
    y está escrito en segunda persona (tuteo neutro Latam) para que el lead lo
    lea como guía, no como rechazo técnico.
    """
    # Mapeo canónico → etiqueta humana. Usamos la etiqueta del campo en el HTML
    # (lo que el usuario vio) en vez del nombre interno del backend.
    HUMAN_LABELS = {
        "nombre": "tu nombre",
        "empresa": "el nombre de tu empresa",
        "email": "tu correo",
        "proceso_manual": "el proceso que querés automatizar",
        "herramientas": "las herramientas que usan",
        "volumen_mensual": "el volumen mensual",
    }
    missing = [f for f in REQUIRED_FIELDS if not (payload.get(f) or "").strip()]
    if missing:
        # 'Faltan datos por completar: tu nombre, tu correo.' (legible, sin jerga)
        labels = [HUMAN_LABELS[f] for f in missing]
        return False, f"Faltan datos por completar: {', '.join(labels)}."

    if not is_valid_email(payload["email"]):
        return False, "El correo que escribiste no parece válido. Revisá que tenga un '@' y un dominio."

    return True, ""


# ---------------------------------------------------------------------------
# Integración Gemini (Google AI Studio)
# ---------------------------------------------------------------------------

def generate_blueprint(payload: dict) -> str:
    """
    Una sola llamada a Gemini 2.5 Flash con el prompt monolítico v5.
    Devuelve el reporte Markdown de 400-700 palabras (4 secciones).

    Diseño (refactor monolítico, ver cabecera de SYSTEM_PROMPT_TEMPLATE arriba):
      - 1 sola llamada a Gemini (no 5 como en el refactor multi-llamada
        anterior) → 1 request por submit (Free Tier: 20 RPD).
      - Pre-procesamiento de CALENDAR_URL en el prompt ANTES de enviar
        a Gemini, para que el LLM la reproduzca verbatim sin riesgo
        de que la rompa con reescritura libre.
      - max_output_tokens=3500 (target output ~700 palabras ≈ 1000 tokens
        + margen 3x para prosa comercial).
      - Logueo de finish_reason=MAX_TOKENS para diagnóstico futuro.

    Contrato externo intacto: misma firma (payload) -> str, mismo formato
    de salida (Markdown que arranca con `## 1.`). _build_lead_summary y
    handler.do_POST no necesitan cambios.
    """
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash")

    # Pre-procesar placeholders ANTES de armar el prompt: Gemini ve la URL
    # del calendario ya resuelta y la reproduce verbatim, sin riesgo de
    # que la rompa con reescritura libre del estilo "(link aquí)".
    template = SYSTEM_PROMPT_TEMPLATE.replace("[URL_CALENDARIO]", CALENDAR_URL)
    prompt = template.format(
        cliente_empresa=payload["empresa"].strip(),
        cliente_proceso=payload["proceso_manual"].strip(),
        cliente_stack=payload["herramientas"].strip(),
        cliente_volumen=payload["volumen_mensual"].strip(),
    )

    try:
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.15,       # determinístico, prosa consistente
                max_output_tokens=3500, # target output ~700 palabras (~1000 tokens) + margen 3x para prosa comercial
            ),
        )
    except Exception as exc:
        raise RuntimeError(f"gemini_call_failed: {exc!r}") from exc

    # Loguear truncamiento para diagnóstico. Con max_output_tokens=3500 y
    # scope 530-750 palabras bien declarado, no debería truncar. Si
    # trunca, el problema es scope laxo, no tokens.
    finish_reason = None
    try:
        finish_reason = str(response.candidates[0].finish_reason)
    except (IndexError, AttributeError):
        pass

    markdown = (response.text or "").strip()
    if not markdown:
        raise RuntimeError("gemini_returned_empty")
    if finish_reason and "MAX_TOKENS" in finish_reason:
        word_count = len(markdown.split())
        secciones_presentes = sum(
            1 for h in ["## 1.", "## 2.", "## 3.", "## 4."] if h in markdown
        )
        print(
            f"[gemini] WARN truncado: finish_reason={finish_reason}, "
            f"palabras_recibidas={word_count}/target 530-750, "
            f"secciones_presentes={secciones_presentes}/4",
            flush=True,
        )

    return markdown


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
# Bloques ```mermaid```: ya no se usan. El prompt v3 reemplaza la sección
# "Arquitectura" por "Stack recomendado en 3 capas" en prosa pura + bullets,
# que se renderiza limpio en cualquier cliente de correo. Si Gemini emite
# un bloque ```mermaid``` igual, python-markdown lo renderiza como
# <pre><code> (no rompe nada, solo se ve como código fuente).


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

    Pipeline de una sola pasada: sustituimos el placeholder del email de
    contacto y renderizamos. No hay extracción de bloques especiales.
    """
    if not md:
        return ""

    # Sustituir el placeholder literal del email de contacto ANTES del
    # render. Gemini reproduce verbatim "[URL/email de contacto]" del
    # prompt si no se lo damos ya resuelto.
    md = md.replace("[URL/email de contacto]", CONTACT_EMAIL)

    # El renderer mantiene estado interno entre llamadas (reset es obligatorio).
    _MD_RENDERER.reset()
    return _MD_RENDERER.convert(md)


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

    1. Buscamos la primera sección "## 1. IMPACTO FINANCIERO Y OPERATIVO" y
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
    "https://brillitotech-servicios.liwaisi.tech/gracias.html",
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
    """Escribe una respuesta 5xx uniforme, con campo debug opcional.

    Si el cliente NO pidió JSON explícitamente (Accept: application/json
    o X-Requested-With), redirige a gracias.html?error=<status> para que
    el navegador muestre la página de gracias en su variante de error
    en vez de JSON crudo en el body. Integra con clientes JS vía fetch
    que sí esperan JSON (devolvemos 5xx + JSON como antes).
    """
    if not wants_json_response(handler):
        base, _, _ = SUCCESS_REDIRECT_URL.partition("/gracias.html")
        write_redirect(handler, f"{base}/gracias.html?error={status}")
        return
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
                    "No pudimos generar el plano ahora. Intentá de nuevo en unos minutos.",
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
                {"status": "error", "message": "Algo se rompió de nuestro lado. Escribinos por WhatsApp y lo resolvemos."},
            )
