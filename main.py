"""
Nøva Garage — backend local para buscar y descargar coches y piezas de Sketchfab.

Arranque:
    pip install -r requirements.txt
    (pon tu token en .env, ver .env.example)
    uvicorn main:app --reload
    abre http://localhost:8000
"""
import asyncio
import io
import json
import os
import re
import uuid
import zipfile
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()
TOKEN = os.getenv("SKETCHFAB_TOKEN", "").strip()
API = "https://api.sketchfab.com/v3"
BASE = Path(__file__).parent
CACHE = BASE / "cache"
CACHE.mkdir(exist_ok=True)
UID_RE = re.compile(r"^[a-f0-9]{32}$")

app = FastAPI(title="Nøva Garage")
client = httpx.AsyncClient(
    timeout=60,
    follow_redirects=True,
    headers={"User-Agent": "NovaGarage/1.0 (uso personal)", "Accept": "application/json"},
)


def check_uid(uid: str) -> None:
    if not UID_RE.match(uid):
        raise HTTPException(400, "UID de modelo no válido")


def only_local(request: Request) -> None:
    if request.client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "Solo disponible desde este equipo")


def pick_thumb(model: dict) -> str | None:
    imgs = (model.get("thumbnails") or {}).get("images") or []
    imgs = sorted(imgs, key=lambda i: abs((i.get("width") or 0) - 400))
    return imgs[0]["url"] if imgs else None


def license_label(model: dict) -> str:
    lic = model.get("license")
    if isinstance(lic, dict):
        return lic.get("label") or lic.get("slug") or "?"
    return lic or "?"


def summarize(m: dict) -> dict:
    return {
        "uid": m["uid"],
        "name": m.get("name"),
        "author": (m.get("user") or {}).get("displayName") or (m.get("user") or {}).get("username"),
        "license": license_label(m),
        "faces": m.get("faceCount"),
        "thumb": pick_thumb(m),
        "url": m.get("viewerUrl") or f"https://sketchfab.com/3d-models/{m['uid']}",
        "cached": (CACHE / m["uid"] / "meta.json").exists(),
    }


@app.get("/api/search")
async def search(
    q: str = Query(..., min_length=1),
    cursor: str | None = None,
    max_faces: int | None = None,
    only_cars: bool = True,
):
    """Búsqueda de respaldo por el backend (la app busca primero desde el navegador)."""
    params = {"type": "models", "q": q, "downloadable": "true", "count": 24, "sort_by": "-likeCount"}
    if only_cars:
        params["categories"] = "cars-vehicles"
    if cursor:
        params["cursor"] = cursor
    if max_faces:
        params["max_face_count"] = max_faces

    headers = {"Authorization": f"Token {TOKEN}"} if TOKEN else {}
    r = None
    for intento in range(5):
        r = await client.get(f"{API}/search", params=params, headers=headers)
        if r.status_code != 202:
            break
        await asyncio.sleep(0.6 * (intento + 1))

    if r.status_code != 200:
        print("Sketchfab search:", r.status_code, r.text[:300])
        raise HTTPException(502, f"Sketchfab respondió {r.status_code}. Prueba de nuevo en unos segundos.")

    data = r.json()
    return {
        "results": [summarize(m) for m in data.get("results", [])],
        "next": (data.get("cursors") or {}).get("next"),
    }


def safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if not str(target).startswith(str(dest)):
            raise HTTPException(400, "El ZIP contiene rutas no válidas")
    zf.extractall(dest)


def find_gltf(folder: Path) -> Path | None:
    found = list(folder.rglob("*.gltf")) or list(folder.rglob("*.glb"))
    return found[0] if found else None


@app.get("/api/token")
def token(request: Request):
    """El navegador pide el token para hablar directamente con Sketchfab (solo local)."""
    only_local(request)
    if not TOKEN:
        raise HTTPException(500, "Falta SKETCHFAB_TOKEN en el archivo .env")
    return {"token": TOKEN}


@app.post("/api/sketchfab/token")
async def set_sketchfab_token(request: Request):
    """La app guarda el token de Sketchfab (ya comprobado por el navegador) en el .env local."""
    global TOKEN
    only_local(request)
    try:
        tok = str(json.loads(await request.body()).get("token", "")).strip()
    except (ValueError, AttributeError):
        raise HTTPException(400, "JSON no válido")
    if not re.fullmatch(r"[A-Za-z0-9]{16,80}", tok):
        raise HTTPException(400, "Eso no parece un token de Sketchfab")
    save_env({"SKETCHFAB_TOKEN": tok})
    TOKEN = tok
    return {"ok": True}


@app.get("/api/models/{uid}")
def cached_model(uid: str):
    """Devuelve el modelo si ya está en caché; 404 si hay que descargarlo."""
    check_uid(uid)
    meta_file = CACHE / uid / "meta.json"
    if not meta_file.exists():
        raise HTTPException(404, "No está en caché")
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    return {**meta, "file": f"/files/{uid}/{meta['entry']}"}


@app.post("/api/models/{uid}/store")
async def store(uid: str, request: Request, meta: str = Query(...)):
    """Recibe el ZIP que ha descargado el navegador, lo descomprime y lo guarda."""
    only_local(request)
    check_uid(uid)
    body = await request.body()
    if not body:
        raise HTTPException(400, "ZIP vacío")
    folder = CACHE / uid
    folder.mkdir(exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            safe_extract(zf, folder)
    except zipfile.BadZipFile:
        raise HTTPException(400, "El archivo recibido no es un ZIP válido")

    entry = find_gltf(folder)
    if not entry:
        raise HTTPException(500, "El ZIP no contiene ningún glTF")
    info = json.loads(meta)
    info.update(uid=uid, entry=entry.relative_to(folder).as_posix(), cached=True)
    (folder / "meta.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**info, "file": f"/files/{uid}/{info['entry']}"}


@app.post("/api/parts/store")
async def store_part(request: Request, meta: str = Query(...)):
    """Guarda una pieza extraída de otro coche (GLB generado en el navegador)."""
    only_local(request)
    return save_part(await request.body(), json.loads(meta))


def save_part(body: bytes, info: dict) -> dict:
    if not body or body[:4] != b"glTF":
        raise HTTPException(400, "El archivo recibido no es un GLB válido")
    uid = uuid.uuid4().hex
    folder = CACHE / uid
    folder.mkdir()
    (folder / "part.glb").write_bytes(body)
    info.update(uid=uid, entry="part.glb", cached=True, kind="part")
    (folder / "meta.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**info, "file": f"/files/{uid}/part.glb"}


@app.post("/api/parts/{uid}/thumb")
async def store_thumb(uid: str, request: Request):
    """Miniatura PNG de una pieza extraída."""
    only_local(request)
    check_uid(uid)
    meta_file = CACHE / uid / "meta.json"
    if not meta_file.exists():
        raise HTTPException(404, "Pieza no encontrada")
    body = await request.body()
    if not body.startswith(b"\x89PNG"):
        raise HTTPException(400, "La miniatura no es un PNG")
    (CACHE / uid / "thumb.png").write_bytes(body)
    info = json.loads(meta_file.read_text(encoding="utf-8"))
    info["thumb"] = f"/files/{uid}/thumb.png"
    meta_file.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"thumb": info["thumb"]}


@app.get("/api/cache")
def cached_models():
    """Coches y piezas ya descargados."""
    out = []
    for f in sorted(CACHE.glob("*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        m = json.loads(f.read_text(encoding="utf-8"))
        out.append({**m, "file": f"/files/{m['uid']}/{m['entry']}"})
    return out


# ---------- builds: coche + pintura + stance + llantas + piezas ----------
BUILDS = CACHE / "builds"
BUILDS.mkdir(exist_ok=True)


def build_file(bid: str) -> Path:
    if not UID_RE.match(bid):
        raise HTTPException(400, "Id de build no válido")
    return BUILDS / f"{bid}.json"


@app.get("/api/builds")
def list_builds():
    """Builds guardados (sin el detalle, solo lo que hace falta para la lista)."""
    out = []
    for f in sorted(BUILDS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        b = json.loads(f.read_text(encoding="utf-8"))
        out.append({k: b.get(k) for k in ("id", "name", "carName", "date", "thumb")})
    return out


@app.get("/api/builds/{bid}")
def get_build(bid: str):
    f = build_file(bid)
    if not f.exists():
        raise HTTPException(404, "Build no encontrado")
    return json.loads(f.read_text(encoding="utf-8"))


@app.post("/api/builds")
async def save_build(request: Request):
    """Guarda un build. Si trae un id existente, lo sobrescribe."""
    only_local(request)
    try:
        b = json.loads(await request.body())
    except ValueError:
        raise HTTPException(400, "JSON no válido")
    if not isinstance(b, dict):
        raise HTTPException(400, "JSON no válido")
    bid = b.get("id")
    if not (isinstance(bid, str) and UID_RE.match(bid)):
        bid = uuid.uuid4().hex
    b["id"] = bid
    build_file(bid).write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
    return {"id": bid}


@app.delete("/api/builds/{bid}")
def delete_build(bid: str, request: Request):
    only_local(request)
    f = build_file(bid)
    if f.exists():
        f.unlink()
    return {"ok": True}


# ---------- catálogo de piezas reales: eBay Browse API ----------
# Claves en .env (https://developer.ebay.com → Application Keys, entorno Production).
EBAY_ID = os.getenv("EBAY_CLIENT_ID", "").strip()
EBAY_SECRET = os.getenv("EBAY_CLIENT_SECRET", "").strip()
EBAY_MARKET = os.getenv("EBAY_MARKETPLACE", "EBAY_ES").strip() or "EBAY_ES"
EBAY_API = "https://api.ebay.com"
# Categoría "piezas de coche" de cada mercado: el filtro de compatibilidad solo funciona dentro de ella
EBAY_PARTS_CAT = {"EBAY_US": "6030", "EBAY_GB": "9800", "EBAY_DE": "9800", "EBAY_ES": "9886",
                  "EBAY_FR": "9886", "EBAY_IT": "9886"}
_ebay_token = {"value": None, "exp": 0.0}


async def ebay_token() -> str:
    """Token de aplicación (client credentials). Dura 2 h; se renueva antes de caducar."""
    import base64
    import time
    if _ebay_token["value"] and time.time() < _ebay_token["exp"] - 120:
        return _ebay_token["value"]
    auth = base64.b64encode(f"{EBAY_ID}:{EBAY_SECRET}".encode()).decode()
    r = await client.post(
        f"{EBAY_API}/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
    )
    if r.status_code != 200:
        print("eBay token:", r.status_code, r.text[:300])
        try:
            err = r.json()
            why = err.get("error_description") or err.get("error") or ""
        except ValueError:
            why = r.text[:120]
        hint = ""
        if "invalid_client" in (why + r.text):
            hint = (" Suele ser el Cert ID mal copiado o que el keyset de Production aún no está activo "
                    "(completa la exención de 'Marketplace Account Deletion' en developer.ebay.com/my/keys).")
        raise HTTPException(502, f"eBay no ha aceptado las claves: {why or r.status_code}.{hint}")
    j = r.json()
    _ebay_token.update(value=j["access_token"], exp=time.time() + int(j.get("expires_in", 7200)))
    return _ebay_token["value"]


def ebay_item(it: dict) -> dict:
    price = it.get("price") or {}
    img = (it.get("image") or {}).get("imageUrl") or next(
        (x.get("imageUrl") for x in it.get("thumbnailImages") or [] if x.get("imageUrl")), None)
    return {
        "id": it.get("itemId"),
        "title": it.get("title"),
        "price": price.get("value"),
        "currency": price.get("currency"),
        "image": img,
        # todas las fotos del anuncio en grande (para recortar la pieza y crear el 3D con IA)
        "images": list(dict.fromkeys(re.sub(r"s-l\d+\.", "s-l1600.", u) for u in
                       [img] + [x.get("imageUrl") for x in it.get("additionalImages") or []] if u))[:8],
        "url": it.get("itemWebUrl"),
        "condition": it.get("condition"),
        "seller": (it.get("seller") or {}).get("username"),
        "compatible": (it.get("compatibilityMatch") or "") in ("EXACT", "POSSIBLE"),
    }


@app.get("/api/ebay/status")
def ebay_status():
    return {"configured": bool(EBAY_ID and EBAY_SECRET), "marketplace": EBAY_MARKET}


def save_env(values: dict) -> None:
    """Escribe o actualiza claves en el .env local (nunca se sube: está en .gitignore)."""
    env = BASE / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    for key, val in values.items():
        row = f"{key}={val}"
        idx = next((i for i, l in enumerate(lines) if l.split("=", 1)[0].strip() == key), None)
        if idx is None:
            lines.append(row)
        else:
            lines[idx] = row
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.post("/api/ebay/config")
async def ebay_config(request: Request):
    """Guarda las claves de eBay desde la app: se prueban contra eBay y, si valen,
    se escriben en el .env y se activan sin reiniciar."""
    global EBAY_ID, EBAY_SECRET, EBAY_MARKET
    only_local(request)
    try:
        body = json.loads(await request.body())
    except ValueError:
        raise HTTPException(400, "JSON no válido")
    cid = str(body.get("client_id", "")).strip()
    secret = str(body.get("client_secret", "")).strip()
    market = str(body.get("marketplace", "") or EBAY_MARKET).strip().upper()
    if not cid or not secret:
        raise HTTPException(400, "Faltan el App ID o el Cert ID")
    if "SBX" in cid.upper() or secret.upper().startswith("SBX"):
        raise HTTPException(400, "Son claves de Sandbox (pruebas). Usa las de Production (llevan PRD).")
    if not re.fullmatch(r"EBAY_[A-Z]{2,3}", market):
        raise HTTPException(400, "Mercado no válido (ej.: EBAY_ES)")
    old = (EBAY_ID, EBAY_SECRET, EBAY_MARKET)
    EBAY_ID, EBAY_SECRET, EBAY_MARKET = cid, secret, market
    _ebay_token.update(value=None, exp=0.0)
    try:
        await ebay_token()   # comprobar que eBay las acepta antes de guardarlas
    except HTTPException:
        EBAY_ID, EBAY_SECRET, EBAY_MARKET = old
        _ebay_token.update(value=None, exp=0.0)
        raise
    save_env({"EBAY_CLIENT_ID": cid, "EBAY_CLIENT_SECRET": secret, "EBAY_MARKETPLACE": market})
    return {"configured": True, "marketplace": market}


@app.get("/api/ebay/search")
async def ebay_search(q: str = Query(..., min_length=1), year: str = "", make: str = "", model: str = "",
                      offset: int = 0):
    """Busca piezas. Con año/marca/modelo intenta primero el filtro de compatibilidad de eBay;
    si el mercado no lo admite o no hay resultados, busca por texto con el coche en la consulta."""
    if not (EBAY_ID and EBAY_SECRET):
        raise HTTPException(503, "Faltan EBAY_CLIENT_ID y EBAY_CLIENT_SECRET en el archivo .env")
    token = await ebay_token()
    headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKET}
    base = {"limit": 24, "offset": max(0, offset)}

    async def run(params: dict):
        r = await client.get(f"{EBAY_API}/buy/browse/v1/item_summary/search", params={**base, **params}, headers=headers)
        if r.status_code != 200:
            print("eBay search:", r.status_code, r.text[:300])
            return None
        return r.json()

    compat = [f"Year:{year}" if year else "", f"Make:{make}" if make else "", f"Model:{model}" if model else ""]
    compat = ";".join(x for x in compat if x)
    data, used = None, False
    if compat and EBAY_MARKET in EBAY_PARTS_CAT:
        data = await run({"q": q, "category_ids": EBAY_PARTS_CAT[EBAY_MARKET], "compatibility_filter": compat})
        used = bool(data and data.get("itemSummaries"))
    if not used:
        car = " ".join(x for x in (make, model) if x)
        data = await run({"q": f"{q} {car}".strip()})
    if data is None:
        raise HTTPException(502, "eBay no ha respondido a la búsqueda")
    items = [ebay_item(it) for it in data.get("itemSummaries") or []]
    return {"results": items, "total": data.get("total", len(items)), "compat": used,
            "next": offset + len(items) if data.get("next") else None}


# ---------- 3D con IA a partir de una foto: Tripo (https://platform.tripo3d.ai) ----------
TRIPO_KEY = os.getenv("TRIPO_API_KEY", "").strip()
TRIPO_API = "https://api.tripo3d.ai/v2/openapi"


def tripo_headers() -> dict:
    if not TRIPO_KEY:
        raise HTTPException(503, "Falta la clave de Tripo")
    return {"Authorization": f"Bearer {TRIPO_KEY}"}


def tripo_data(r: httpx.Response, what: str) -> dict:
    """Respuesta de Tripo: {code: 0, data: {...}}; cualquier otra cosa es un error legible."""
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code != 200 or j.get("code", 0) != 0:
        print(f"Tripo {what}:", r.status_code, r.text[:300])
        msg = j.get("message") or j.get("suggestion") or f"HTTP {r.status_code}"
        if r.status_code in (401, 403):
            msg = "Tripo no acepta la clave (revísala en platform.tripo3d.ai → API Keys)"
        raise HTTPException(502, f"Tripo ({what}): {msg}")
    return j.get("data") or {}


@app.get("/api/tripo/status")
async def tripo_status():
    if not TRIPO_KEY:
        return {"configured": False}
    try:
        d = tripo_data(await client.get(f"{TRIPO_API}/user/balance", headers=tripo_headers()), "saldo")
        return {"configured": True, "balance": d.get("balance")}
    except HTTPException as e:
        return {"configured": True, "error": e.detail}


@app.post("/api/tripo/config")
async def tripo_config(request: Request):
    """Guarda la clave de Tripo en el .env local tras comprobarla (consultando el saldo)."""
    global TRIPO_KEY
    only_local(request)
    try:
        key = str(json.loads(await request.body()).get("key", "")).strip()
    except (ValueError, AttributeError):
        raise HTTPException(400, "JSON no válido")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{16,120}", key):
        raise HTTPException(400, "Eso no parece una clave de Tripo (suele empezar por tsk_)")
    old, TRIPO_KEY = TRIPO_KEY, key
    try:
        d = tripo_data(await client.get(f"{TRIPO_API}/user/balance", headers=tripo_headers()), "clave")
    except HTTPException:
        TRIPO_KEY = old
        raise
    save_env({"TRIPO_API_KEY": key})
    return {"configured": True, "balance": d.get("balance")}


@app.get("/api/img")
async def image_proxy(url: str):
    """Trae una foto de eBay a través del servidor para poder recortarla en el navegador (CORS)."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if not (host == "i.ebayimg.com" or host.endswith(".ebayimg.com")):
        raise HTTPException(400, "Solo se admiten fotos de eBay")
    r = await client.get(url)
    if r.status_code != 200:
        raise HTTPException(502, "No se pudo descargar la foto")
    from fastapi.responses import Response
    return Response(r.content, media_type=r.headers.get("content-type", "image/jpeg"))


@app.post("/api/tripo/generate")
async def tripo_generate(request: Request):
    """Recibe el recorte (PNG/JPEG) de la pieza, lo sube a Tripo y lanza la tarea image_to_model."""
    only_local(request)
    body = await request.body()
    kind = "png" if body[:4] == b"\x89PNG" else "jpg" if body[:2] == b"\xff\xd8" else None
    if not kind:
        raise HTTPException(400, "La imagen debe ser PNG o JPEG")
    up = None
    for path in ("/upload", "/upload/sts"):   # según la versión de la API
        r = await client.post(f"{TRIPO_API}{path}", headers=tripo_headers(),
                              files={"file": (f"pieza.{kind}", body, f"image/{'png' if kind == 'png' else 'jpeg'}")})
        if r.status_code != 404:
            up = tripo_data(r, "subida")
            break
    token = up and (up.get("image_token") or up.get("file_token") or up.get("token"))
    if not token:
        raise HTTPException(502, "Tripo no ha devuelto la imagen subida")
    task = {"type": "image_to_model", "file": {"type": kind, "file_token": token}, "texture": True, "pbr": True}
    d = tripo_data(await client.post(f"{TRIPO_API}/task", headers=tripo_headers(), json=task), "tarea")
    return {"task_id": d.get("task_id")}


def tripo_url(v) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return v.get("url")
    return None


@app.get("/api/tripo/task/{task_id}")
async def tripo_task(task_id: str, request: Request, meta: str = Query("{}")):
    """Progreso de la tarea. Al terminar se descarga el GLB (Tripo lo borra a los 5 min) y se
    guarda como pieza propia de la biblioteca."""
    only_local(request)
    if not re.fullmatch(r"[A-Za-z0-9\-]{8,80}", task_id):
        raise HTTPException(400, "Tarea no válida")
    d = tripo_data(await client.get(f"{TRIPO_API}/task/{task_id}", headers=tripo_headers()), "estado")
    status = d.get("status")
    if status != "success":
        return {"status": status, "progress": d.get("progress", 0)}
    out = d.get("output") or {}
    url = tripo_url(out.get("pbr_model")) or tripo_url(out.get("model")) or tripo_url(out.get("base_model"))
    if not url:
        raise HTTPException(502, "Tripo ha terminado pero no ha dado el modelo")
    r = await client.get(url)
    if r.status_code != 200:
        raise HTTPException(502, "No se pudo descargar el modelo de Tripo")
    info = json.loads(meta)
    info.update(local=True, ai=True, author="Generado con IA (Tripo)", license="propia")
    part = save_part(r.content, info)
    thumb = tripo_url(out.get("rendered_image"))
    if thumb:
        try:
            t = await client.get(thumb)
            if t.status_code == 200:
                ext = "webp" if t.content[:4] == b"RIFF" else "png" if t.content[:4] == b"\x89PNG" else "jpg"
                (CACHE / part["uid"] / f"thumb.{ext}").write_bytes(t.content)
                part["thumb"] = f"/files/{part['uid']}/thumb.{ext}"
                meta_file = CACHE / part["uid"] / "meta.json"
                m = json.loads(meta_file.read_text(encoding="utf-8")); m["thumb"] = part["thumb"]
                meta_file.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        except httpx.HTTPError:
            pass
    return {"status": "success", "part": part}


app.mount("/files", StaticFiles(directory=CACHE), name="files")


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")
