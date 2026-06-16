# brillitotech.github.io

Landing page eco-eficiente de marca personal — HTML5 semántico + CSS3, **cero JavaScript**, optimizada para GitHub Pages.

## Stack y principios

- **HTML5 semántico** + **CSS3 interno** en `<head>` (una sola petición HTTP).
- **Mobile-First** con CSS Grid + Flexbox.
- **0 bytes de JavaScript**.
- **System fonts** (sin peticiones de fuentes externas).
- **Paleta Modo Oscuro Sostenible**: Navy `#1A2C44` · Hielo `#E1EBF5` · Verde `#5FB878` · Coral `#F0626A` · Cards `#114155`.

## Regla de peso (no negociable)

La página entera debe pesar **menos de 200 KB** total (HTML + CSS + imágenes + todo).
Imágenes **exclusivamente** en **WebP** o **SVG inline**.

Verificar antes de commit:

```bash
# Tamaño total de la página servida
wc -c index.html

# Inventario de assets
find . -type f \( -name "*.jpg" -o -name "*.jpeg" -o -name "*.png" -o -name "*.gif" -o -name "*.webp" -o -name "*.svg" \) -not -path "./.git/*" -exec du -h {} +
```

Si aparece un `.png` o `.jpg` → convertir a WebP antes de commitear:

```bash
cwebp -q 80 input.png -o output.webp
```

## Despliegue local para pruebas

El sitio es 100% estático. Cualquier servidor HTTP local sirve. Tres opciones de menor a mayor overhead:

### Opción 1 · Python (ya viene en macOS/Linux)

```bash
# Desde la raíz del repo
python3 -m http.server 8000
```

Abrir <http://localhost:8000>

### Opción 2 · npx (si tenés Node instalado, sin instalar nada)

```bash
npx serve .
```

Abre automáticamente el puerto (generalmente 3000).

### Opción 3 · PHP (si lo tenés)

```bash
php -S localhost:8000
```

### Probar contra la URL final de GitHub Pages

Una vez pusheado a `main`, el sitio queda disponible en:

```
https://brillitotech.github.io/
```

Para previsualizar el resultado **idéntico** al de producción:

1. Push a `main`.
2. Esperar ~30 s a que GitHub Pages compile.
3. Abrir la URL pública en modo incógnito (para evitar caché).

## Estructura del proyecto

```
brillitotech.github.io/
├── index.html        # Landing completa (HTML + CSS interno)
├── README.md         # Este archivo
└── .atl/             # Convenciones del agente
```

## Despliegue a producción

El deploy es automático: `git push` a `main` → GitHub Pages publica en `https://brillitotech.github.io/`.

Configurar (solo la primera vez): **Settings → Pages → Source: Deploy from a branch → Branch: main / (root)**.
