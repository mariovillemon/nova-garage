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
    body = await request.body()
    if not body or body[:4] != b"glTF":
        raise HTTPException(400, "El archivo recibido no es un GLB válido")
    uid = uuid.uuid4().hex
    folder = CACHE / uid
    folder.mkdir()
    (folder / "part.glb").write_bytes(body)
    info = json.loads(meta)
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
        raise HTTPException(502, "eBay no ha aceptado las claves (revisa EBAY_CLIENT_ID y EBAY_CLIENT_SECRET)")
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


app.mount("/files", StaticFiles(directory=CACHE), name="files")


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")
