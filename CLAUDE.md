# Nøva Garage — configurador de modificaciones de coches

Proyecto personal (no comercial) de Mario. Responde y comenta el código en **castellano**.

## Qué es
Configurador web 3D: se busca un coche en Sketchfab, se descarga y se modifica
(pintura, acabados, llantas, stance, alerones, escapes, aero, carrocería).
Plan: web ahora, versión Flutter después reutilizando los mismos GLB/JSON.

## Arranque
```
pip install -r requirements.txt
cp .env.example .env        # y poner SKETCHFAB_TOKEN
uvicorn main:app --reload   # http://localhost:8000
```
`.env` y `cache/` están en `.gitignore`: **nunca** subir el token ni los modelos descargados.

## Arquitectura
- `main.py` — backend FastAPI local.
  - `GET /api/token` (solo localhost): el navegador pide el token.
  - `GET /api/models/{uid}`: metadatos si está en caché, 404 si no.
  - `POST /api/models/{uid}/store?meta=…`: recibe el ZIP glTF descargado por el navegador y lo descomprime en `cache/{uid}/`.
  - `POST /api/parts/store?meta=…`: guarda un GLB generado en el navegador (piezas extraídas); `POST /api/parts/{uid}/thumb`: su miniatura PNG.
  - `GET /api/cache`: todo lo descargado. `meta.kind` = `car` | `part`; las piezas llevan `category`.
  - `/files/…` sirve `cache/`.
  - `GET /api/search`: búsqueda de respaldo (casi nunca funciona, ver abajo).
- `static/index.html` — todo el frontend en un único archivo (HTML + CSS + JS).
  three.js **r147** por CDN (jsdelivr, builds UMD de `examples/js`): OrbitControls,
  GLTFLoader, RoomEnvironment, TransformControls, GLTFExporter.

## Decisiones importantes (no deshacer sin motivo)
- **Sketchfab bloquea con anti-bots** (responde 202 vacío) las peticiones desde Python.
  Por eso búsqueda y descarga se hacen **desde el navegador** (`searchDirect`, `downloadModel`);
  el backend solo guarda y sirve archivos.
- Color: `THREE.ColorManagement.legacyMode = false` y `LinearToneMapping`. Sin esto los
  hex salen mucho más claros. Luz: entorno + un sol suave (sin HemisphereLight).
- Todo modelo se normaliza a **4,4 m de largo**, centrado y apoyado en el suelo.
  `axes = {len, lat}` indica qué eje es el largo y cuál el ancho del coche.
- Cada malla guarda `userData.orig` (material original), `userData.origWorld`
  (matriz mundial original) y `userData.role`.
- Roles: `body`, `wheels`, `calipers`, `glass`, `chrome`, `tire`, `lights`, `other`, `hide`.
  Detección por nombre en `guessRole` + comprobaciones geométricas. `applyRole` asigna
  material compartido por rol; con materiales múltiples decide por ranura.
- `applyLow()` aplica rebaje, camber y separadores calculando la posición final en
  el mundo desde `origWorld` (funciona con jerarquías anidadas). Las ruedas se
  agrupan por esquina en `buildWheelGroups()` (`D`/`T` + `+`/`-`).
- `partitionMesh(m, keyOf)` parte una malla por triángulos conservando materiales;
  lo usan: separar ruedas fusionadas, separar neumático (por radio o por color de
  textura), separar en trozos sueltos (`splitIslands`) y recortar zona (`applyCut`).
- Packs (`splitPack`): se trocean por **huecos en una rejilla de vóxeles**, marcando la
  superficie completa de cada triángulo. Si una zona tiene >60 % de los triángulos se
  considera pieza única, no pack.
- Piezas añadidas cuelgan de `addonRoot` (baja con el rebaje). Llantas nuevas: plantilla
  normalizada (`prepareRim`) + neumático generado con `LatheGeometry`; mantienen el
  diámetro exterior original del coche.
- Modo coche donante: en la pestaña Carrocería los resultados se abren como coche;
  "Guardar como pieza" exporta la selección con GLTFExporter en coordenadas del coche
  (`meta.extracted = true`) para que en el mismo modelo caiga en su sitio.

## Pendiente (por prioridad)
1. **Piezas generadas desde la forma del coche** (labio delantero, faldones, difusor,
   aletines/overfenders) usando raycast sobre la carrocería, con controles de tamaño.
2. **Guardar builds**: coche + colores + acabados + stance + llantas + piezas añadidas
   (posición) + piezas ocultas/recortadas. Hoy se pierde todo al cambiar de coche.
3. Indicador de "calidad para personalizar" al cargar un modelo (% de piezas reconocidas).
4. Catálogo de mods reales vía **eBay Browse API** (filtro de compatibilidad por vehículo).
5. Versión Flutter.

## Cómo trabajar
- Mario prueba en Windows con Chrome; valida los cambios él mismo y manda capturas.
- Tras cambiar JS, comprobar sintaxis extrayendo el `<script>` y `node --check`.
- Mantener el frontend en un solo `index.html` salvo que se decida separar módulos.
