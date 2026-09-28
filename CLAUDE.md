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
  - `GET/POST /api/builds`, `GET/DELETE /api/builds/{id}`: builds guardados en `cache/builds/{id}.json`.
  - `GET /api/cache`: todo lo descargado. `meta.kind` = `car` | `part`; las piezas llevan `category`.
  - `/files/…` sirve `cache/`.
  - `GET /api/search`: búsqueda de respaldo (casi nunca funciona, ver abajo).
- `static/index.html` — todo el frontend en un único archivo (HTML + CSS + JS).
  three.js **r147** por CDN (jsdelivr, builds UMD de `examples/js`): OrbitControls,
  GLTFLoader, RoomEnvironment, TransformControls, GLTFExporter, DecalGeometry.

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

- Piezas a medida (`GEN`, `GEN_SAMPLE`, `GEN_BUILD`): labio, faldones, difusor y aletines.
  Se mide la carrocería con rayos paralelos a un eje sobre `rayGrid` (rejilla 2D de triángulos
  en coordenadas `origWorld`, sin rebajar = coordenadas de `addonRoot`); el raycaster de three
  es demasiado lento para modelos grandes. La forma se construye con `sweepGeometry` (secciones
  barridas). Las mediciones se guardan en `userData.gen` y los controles solo reconstruyen.
- Builds: `ops` registra lo que se hace al modelo (roles, recortes, trozos, neumáticos, colores),
  identificando las piezas por su índice en `baseMeshes()`. Al cargar un build se repiten las
  operaciones en orden (`replaying = true`), luego estado, llantas y piezas con su transformación.
  Las piezas de un pack guardan `packIndex`.
- Colocación automática de piezas de biblioteca (`mountAddon`): `orientPart` gira la pieza por
  su forma (ancho a lo ancho; alerón: lo ancho arriba y el ala subiendo hacia atrás; escape: tubo
  a lo largo y boca gorda atrás) y `placePart` la mide contra el coche: alerón en el borde del
  maletero (`findDeck`) al ancho de la carrocería y apoyado por sus patas (`seatOnBody`); escape y
  aero bajo el paragolpes (`sampleBumper`). Si no se puede medir, vuelve a la colocación por caja.
- Pinzas por forma (`detectCalipers`, se ejecuta al cargar): dentro de cada rueda, un trozo suelto
  (`meshIslands`) que ocupa ≤130° de arco, no llega al buje ni al neumático y va por dentro de la
  cara de la llanta es pinza; si viene fusionada con disco/llanta se parte.
- Vinilos (`VINYL`, `buildVinyl`): textura dibujada en canvas (franjas, número, texto o PNG
  reducido a 1024 px) proyectada con `DecalGeometry` sobre las piezas `body`; se descartan los
  triángulos que no miran al proyector (`keepFacing`). Cada vinilo guarda punto y normal en
  coordenadas del coche sin rebajar; cuelgan de `vinylRoot`, que baja con `applyLow`. Las franjas
  se proyectan desde arriba a lo largo de todo el coche. Van en el build (`vinylSave/vinylLoad`).
- Wrap completo desde plantilla de rotulista: la imagen (hasta 3000 px, JPEG) se guarda una vez
  en el build (`wrapSrc`); cada zona (`WRAP_ZONES`: laterales, capó, techo, maletero, frontal,
  trasera) es un vinilo `wrap` con su recorte, giro en pasos de 90° y espejo, proyectado desde su
  lado sobre toda la zona. Capó/techo/maletero ocupan un tramo del largo ajustable. Las imágenes
  con marcas de terceros son del usuario: no se suben al repo.
- Builds: `buildData()` define el build; `applyBuild(b, reload)` lo aplica (con `reload` recarga el
  coche y repite `ops`; sin él solo rehace estado, llantas, piezas y vinilos). Lo usan cargar,
  importar y deshacer. Exportar mete dentro del JSON las piezas propias (`extracted`/`local`) en
  base64; al importar se suben de nuevo. Coches y piezas de Sketchfab que falten se descargan solos.
- Deshacer/rehacer (`hist`): foto de `buildData()` tras cada clic/cambio y cada 1,2 s si cambió.
- Antes/después (`renderCompare`): dos pasadas con scissor; la de serie pone cada malla en
  `origWorld` con su material original y oculta piezas añadidas y vinilos.
- Escenarios (`SCENES`, `applyScene`), captura al doble de resolución (`capture`), faros
  (`lampMaterial` en `applyRole` + focos en `lampGroup`), rake (`rakeMatrix` en `applyLow`, gira
  carrocería, `addonRoot` y `vinylRoot`), extras de llanta (`wheelExtra`: ancho, offset, labio y
  letras como anillos hijos del neumático), pintura por zonas (vinilo `tone`: color o carbono).
- `updateQuality()` muestra bajo el nombre del coche la calidad para personalizar.

## Pendiente (por prioridad)
1. Catálogo de mods reales vía **eBay Browse API** (filtro de compatibilidad por vehículo).
2. Versión Flutter.

## Cómo trabajar
- Mario prueba en Windows con Chrome; valida los cambios él mismo y manda capturas.
- Tras cambiar JS, comprobar sintaxis extrayendo el `<script>` y `node --check`.
- Mantener el frontend en un solo `index.html` salvo que se decida separar módulos.
