# Nøva Garage — configurador con Sketchfab (uso personal)

## Puesta en marcha
1. `pip install -r requirements.txt`
2. Copia `.env.example` a `.env` y pega tu token de https://sketchfab.com/settings/password (apartado "API Token").
3. `uvicorn main:app --reload`
4. Abre http://localhost:8000

## Cómo funciona
- `GET  /api/search?q=celica` → busca modelos descargables (por defecto solo coches).
- `POST /api/models/{uid}/prepare` → descarga el glTF, lo descomprime en `cache/{uid}/` y devuelve la ruta. Cada modelo se descarga una sola vez.
- `GET  /api/cache` → coches ya descargados.
- `/files/...` → sirve los modelos descargados al visor.

El token solo vive en el backend; el navegador nunca lo ve.
Cada modelo guarda autor, licencia y enlace en `cache/{uid}/meta.json`, y el visor muestra el crédito.
