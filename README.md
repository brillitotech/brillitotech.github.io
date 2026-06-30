# brillitotech-servicios.liwaisi.tech

Landing page eco-eficiente de marca personal + backend serverless para captura de leads.

El sitio se vende bajo el principio de **alta eficiencia y baja huella digital**, y lo demuestra en su propia construcción: cero frameworks, cero JavaScript en el cliente, <200 KB total en el frontend, y un backend que solo cobra vida en milisegundos cuando alguien envía el formulario.

---

## Stack

### Frontend (eco-eficiente)

- **HTML5 semántico** + **CSS3** en archivos separados por responsabilidad (`tokens`, `base`, `layout`, `components`, `themes`).
- **0 bytes de JavaScript** en el cliente. Todo el comportamiento (toggle de tema, modal del formulario) se resuelve con CSS puro: `<input type="checkbox">` + selector de hermanos `~`.
- **System fonts** (sin peticiones a Google Fonts ni CDNs externos).
- **Paleta de marca** (definida en `css/tokens.css`, single source of truth):
  - Verde Abismo `#0D1B1E` · fondo
  - Verde Tierra `#103035` · tarjetas
  - Blanco Nube `#F9F9F9` · texto
  - Gris Técnico `#8E9794` · bordes, texto secundario
  - Verde Clorofila `#2ECC71` · CTAs y acentos (≤20%)

### Backend (serverless, mínimo consumo)

- **Python 3.12 nativo** sin frameworks web (sin FastAPI, sin Flask).
- **`http.server.BaseHTTPRequestHandler`** de la stdlib → cold-start y RAM mínimos en runtime.
- **Gemini 2.5 Flash** para generar el plano técnico en Markdown (modelo elegido por menor energía/token vs Pro).
- **Resend** para despachar el correo al lead + copia interna a la dueña.
- Vive en la carpeta `/api` que Vercel detecta automáticamente y monta en `POST /api`.

---

## Regla de peso del frontend (no negociable)

La página entera debe pesar **menos de 200 KB** total (HTML + CSS + imágenes).
Imágenes **exclusivamente** en **WebP** o **SVG inline**.

Verificar antes de commit:

```bash
wc -c index.html gracias.html css/*.css
find . -type f \( -name "*.jpg" -o -name "*.jpeg" -o -name "*.png" -o -name "*.gif" -o -name "*.webp" -o -name "*.svg" \) -not -path "./.git/*" -exec du -h {} +
```

Si aparece un `.png` o `.jpg` → convertir a WebP antes de commitear:

```bash
cwebp -q 80 input.png -o output.webp
```

---

## Estructura del proyecto

```
brillitotech.github.io/
├── index.html             # Landing + modal de diagnóstico
├── gracias.html           # Página estática de confirmación post-submit
├── css/                   # Estilos del frontend (tokens → themes)
│   ├── tokens.css
│   ├── base.css
│   ├── layout.css
│   ├── components.css
│   └── themes.css
├── assets/                # Imágenes WebP / SVG inline
├── api/                   # Backend serverless (Vercel)
│   ├── index.py           # Handler BaseHTTPRequestHandler → POST /api
│   └── requirements.txt   # google-generativeai + requests
├── .atl/                  # Convenciones del agente
└── README.md              # Este archivo
```

---

## Contrato del endpoint `POST /api`

El formulario del modal (`index.html` → sección 5) envía los 6 campos al endpoint.

### Request

- **Content-Type**: `application/x-www-form-urlencoded` (default del `<form>`) o `application/json`.
- **Campos obligatorios** (nombres del wire del HTML):

| Wire (HTML) | Canónico (backend → Gemini) | Tipo |
|---|---|---|
| `nombre` | `nombre` | string |
| `empresa` | `empresa` | string |
| `correo` | `email` | email válido |
| `proceso` | `proceso_manual` | string (≤300 chars) |
| `stack` | `herramientas` | string |
| `volumen` | `volumen_mensual` | enum: `lt-100`, `100-1000`, `1000-10000`, `gt-10000` |

> **Por qué hay un mapeo wire→canónico**: el HTML desplegado usa `correo/proceso/stack/volumen`; el brief del backend pide `email/proceso_manual/herramientas/volumen_mensual`. El handler normaliza al cruzar la frontera. Si en el futuro renombras los inputs en HTML, actualiza `WIRE_TO_CANONICAL` en `api/index.py`.

### Responses

| Caso | Status | Body | Comportamiento del navegador |
|---|---|---|---|
| Éxito (form HTML) | `302 Found` | (vacío) | Header `Location` redirige a `/gracias.html` |
| Éxito (cliente JSON) | `200 OK` | `{"status": "success", "message": "Plano técnico enviado con éxito."}` | Muestra el JSON |
| Validación | `400 Bad Request` | `{"status": "error", "message": "Campos requeridos faltantes: ..."}` | Se queda en la página (no redirige) |
| Falla Gemini | `500 Internal Server Error` | `{"status": "error", "message": "No fue posible generar el plano técnico..."}` | Se queda en la página |
| Falla Resend | `500 Internal Server Error` | `{"status": "error", "message": "Generamos tu plano pero falló el envío..."}` | Se queda en la página |
| Falla inesperada | `500 Internal Server Error` | `{"status": "error", "message": "Error inesperado procesando la solicitud."}` | Se queda en la página |

**Detección de cliente JSON vs HTML**: el handler considera que el cliente quiere JSON si la request incluye `Accept: application/json` o `X-Requested-With: fetch`. Si no, hace `302` con `Location: <SUCCESS_REDIRECT_URL>`.

### Flujo del plano técnico

1. Parseo del body + normalización wire→canónico.
2. Validación de presencia y formato de los 6 campos.
3. `gemini-2.5-flash` con prompt de sistema de "Arquitecto de Soluciones Cloud" → reporte en Markdown con diagnóstico financiero, stack recomendado en 3 capas y próximos pasos. **Sin bloques Mermaid**: la propuesta se entrega en prosa + bullets, que se renderiza limpio en Gmail, Outlook y Apple Mail sin necesidad de servicios externos.
4. Conversión del Markdown a HTML con `python-markdown` (extensiones: `fenced_code`, `tables`, `sane_lists`, `nl2br`).
5. `Resend` API → email al cliente con `text/plain` (Markdown) + `text/html`; segundo destinatario `OWNER_NOTIFICATION_EMAIL` para notificación de lead.

---

## Variables de entorno (Vercel → Project Settings → Environment Variables)

| Variable | Requerida | Default | Propósito |
|---|---|---|---|
| `GEMINI_API_KEY` | ✅ | — | API key de Google AI Studio |
| `RESEND_API_KEY` | ✅ | — | API key de Resend |
| `OWNER_NOTIFICATION_EMAIL` | recomendada | — | Email que recibe copia de cada lead (lead-notification) |
| `EMAIL_FROM` | opcional | `Brillitotech <no-reply@brillitotech.com>` | Remitente visible de los correos |
| `SUCCESS_REDIRECT_URL` | opcional | `https://brillitotech-servicios.liwaisi.tech/gracias.html` | URL del 302 en éxito (form HTML). **Cambia este valor si tu dominio de deploy es distinto.** |
| `CONTACT_EMAIL` | opcional | `btatacc@gmail.com` | Email que aparece en el CTA final del reporte ("respondé este correo o escribí directamente a…"). Si no la seteás, usa el fallback. |

---

## Despliegue local

### Frontend solo (sin backend)

Para revisar cambios de UI sin tocar Python:

```bash
cd brillitotech.github.io
python3 -m http.server 8000
# Abrir http://localhost:8000
```

> El formulario NO funcionará en este modo (las llamadas a `/api` devolverán `404` o `501`). Es solo para iterar UI.

### Probar el submit end-to-end con backend real

Requiere las dos API keys en el entorno:

```bash
# Terminal 1: servir el frontend
python3 -m http.server 8000

# Terminal 2: servir el handler Python localmente (solo para smoke test)
cd brillitotech.github.io/api
GEMINI_API_KEY="tu_key" \
RESEND_API_KEY="tu_key" \
OWNER_NOTIFICATION_EMAIL="tu@correo.com" \
python3 -m http.server 8001
# Esto NO ejecuta el handler; Vercel es quien lo hace en producción.
# El smoke test real del handler es con `curl` (abajo).
```

### Smoke test del endpoint con `curl`

```bash
curl -i -X POST http://localhost:8000/api \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "nombre=Briggitte+Tatiana&empresa=Brillitotech&correo=test@brillitotech.com&proceso=conciliar+facturas&stack=Excel%2CGmail&volumen=100-1000"
```

**Esperado** (con API keys configuradas en el deploy):
- `HTTP/1.1 302 Found`
- `Location: https://brillitotech-servicios.liwaisi.tech/gracias.html` (o la `SUCCESS_REDIRECT_URL` configurada)

**Esperado** (cliente que pide JSON):
```bash
curl -i -X POST http://localhost:8000/api \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "Accept: application/json" \
  -d "..."
# → HTTP/1.1 200 OK + {"status": "success", ...}
```

---

## Despliegue a producción

El deploy es **automático vía Vercel**: `git push` a `main` → Vercel compila, monta el frontend estático en la raíz y las funciones de `/api` en `POST /api`.

### Configuración inicial (solo la primera vez)

1. **Importar el repo en Vercel** (https://vercel.com/new).
2. **Environment Variables**: configurar `GEMINI_API_KEY`, `RESEND_API_KEY`, `OWNER_NOTIFICATION_EMAIL` en Project Settings → Environment Variables (Production, Preview y Development). Opcional: `CONTACT_EMAIL` (si querés que el CTA final del reporte muestre un mail distinto al default `btatacc@gmail.com`).
3. **Dominio custom** (opcional): si no es `brillitotech-servicios.liwaisi.tech`, setear `SUCCESS_REDIRECT_URL` apuntando a la URL real de `gracias.html` en el deploy del servicio (ej. `https://otro-dominio.com/gracias.html`).
4. **Deploy**.

> **Nota importante**: el backend `/api` NO funciona en GitHub Pages. Si en algún momento movés el frontend a GitHub Pages, el formulario quedará inerte. La decisión de deploy debe ser Vercel (o cualquier plataforma que soporte Python serverless en `/api`).

---

## Convenciones del proyecto

- **Idioma de artifacts**: inglés (código, comentarios técnicos, nombres de variables, mensajes de commit). Conversación: español.
- **Conventional Commits** sin `Co-Authored-By`.
- **Cero números hardcoded de rendimiento** sin auditoría. Si no hay medición, marcar como `[medir]` o pedir el dato.
- **Cero testimonios fabricados**: si un bloque pide testimonio, dejar placeholder honesto marcado como pendiente.
- **Regla <200 KB** del frontend (ver arriba).
