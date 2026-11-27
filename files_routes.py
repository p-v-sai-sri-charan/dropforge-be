"""Image uploads (Cloudflare R2 or Cloudinary, see storage.py) + owner-checked downloads and resized thumbnails."""
import io
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import cloudinary
import cloudinary.uploader
import cloudinary.utils
import requests
from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

import storage
from core import check_pin, client_ip, current_user, db, is_admin_phone, optional_user

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)

APP_NAME = "dropforge"
MAX_BYTES = 8 * 1024 * 1024


def cloudinary_ready() -> bool:
    name, key, secret = (os.environ.get(k) for k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"))
    if not (name and key and secret):
        return False
    cloudinary.config(cloud_name=name, api_key=key, api_secret=secret, secure=True)
    return True


def cld_upload(data: bytes, owner_id: str, public: bool) -> dict:
    # Private uploads use Cloudinary "authenticated" delivery: only reachable via signed URLs from this backend.
    return cloudinary.uploader.upload(
        io.BytesIO(data),
        resource_type="image",
        type="upload" if public else "authenticated",
        folder=f"{APP_NAME}/{'public' if public else 'private'}/{owner_id}",
        public_id=str(uuid.uuid4()),
        overwrite=False,
    )


def cld_fetch(rec: dict) -> bytes:
    url, _ = cloudinary.utils.cloudinary_url(
        rec["public_id"], resource_type="image", type=rec["delivery_type"], format=rec["format"], sign_url=True, secure=True
    )
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


def fetch_bytes(rec: dict) -> bytes:
    """Original bytes of a stored file, wherever it lives (blocking: call in a thread)."""
    if rec.get("provider") == "r2":
        return storage.r2_get(rec["bucket"], rec["key"])
    cloudinary_ready()
    return cld_fetch(rec)


def _thumb_place(rec: dict, w: int):
    """(bucket, key) for a variant: next to an R2 original; Cloudinary originals get theirs in the private bucket."""
    if rec.get("provider") == "r2":
        return rec["bucket"], storage.thumb_key(rec["key"], w)
    return storage.bucket(False), storage.thumb_key(rec["path"], w)


def _delete_stored(rec: dict):
    for w in rec.get("thumbs", []):
        b, k = _thumb_place(rec, w)
        storage.r2_delete(b, [k])
    if rec.get("provider") == "r2":
        storage.r2_delete(rec["bucket"], [rec["key"]])
    else:
        cloudinary.uploader.destroy(rec["public_id"], resource_type="image", type=rec["delivery_type"], invalidate=True)


async def store_file(data: bytes, ct: str, owner_id: str, public: bool, png_only: bool = False) -> dict:
    ALLOWED = {"image/png", "image/jpeg", "image/webp", "image/gif"}
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Image is too large (max 8 MB)")
    if png_only and not (ct == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(400, "Only PNG files are accepted. Export your artwork as PNG and try again.")
    if ct not in ALLOWED or not (
        data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP") or data[:3] == b"GIF"
    ):
        raise HTTPException(400, "Only PNG, JPEG, WEBP or GIF images can be uploaded")
    rec = {
        "id": str(uuid.uuid4()),
        "owner_id": owner_id,
        "content_type": ct,
        "size": len(data),
        "public": public,
        "is_deleted": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    backend = storage.upload_backend()
    if not backend:
        raise HTTPException(503, "Image storage is not configured")
    try:
        if backend == "r2":
            ext = EXT[ct]
            key = f"{'public' if public else 'private'}/{owner_id}/{rec['id']}.{ext}"
            b = storage.bucket(public)
            await run_in_threadpool(storage.r2_put, b, key, data, ct, "public, max-age=31536000, immutable" if public else "private, max-age=86400")
            path = f"r2/{rec['id']}.{ext}"
            rec.update(path=path, provider="r2", bucket=b, key=key)
        else:
            res = await run_in_threadpool(cld_upload, data, owner_id, public)
            path = f"cld/{res['public_id']}.{res['format']}"
            rec.update(path=path, provider="cloudinary", public_id=res["public_id"], format=res["format"],
                       delivery_type=res.get("type", "upload"), cloud_url=res["secure_url"])
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("%s upload failed: %s", backend, e)
        raise HTTPException(502, "Upload failed, try again")
    await db.files.insert_one(rec)
    return {"path": path, "url": f"/api/files/{path}"}


async def _in_use(src: str) -> bool:
    checks = [
        (db.orders, ["photo", "design_snapshot.front.src", "design_snapshot.back.src", "design_snapshot.thumb", "design_snapshot.mockup", "design_snapshot.photos",
                      "design_snapshot.design.front.src", "design_snapshot.design.back.src", "design_snapshot.design.thumb", "design_snapshot.design.mockup"]),
        (db.designs, ["front.src", "back.src", "thumb", "mockup"]),
        (db.catalog_products, ["photos", "design.front.src", "design.back.src", "design.thumb", "design.mockup"]),
        (db.shares, ["front_image", "back_image", "design.front.src", "design.back.src", "design.thumb", "design.mockup"]),
    ]
    for coll, fields in checks:
        if await coll.find_one({"$or": [{f: src} for f in fields]}, {"_id": 1}):
            return True
    return False


async def release_files(srcs) -> int:
    """Delete stored images no longer used anywhere. Anything an order still needs is kept for printing.
    Call AFTER removing the owning document."""
    removed = 0
    for src in {s for s in srcs if isinstance(s, str) and s.startswith("/api/files/")}:
        if await _in_use(src):
            continue
        rec = await db.files.find_one({"path": src[len("/api/files/"):], "is_deleted": False})
        if not rec:
            continue
        if (rec.get("provider") == "r2" and storage.r2_ready()) or (rec.get("provider") == "cloudinary" and cloudinary_ready()):
            try:
                await run_in_threadpool(_delete_stored, rec)
            except Exception as e:
                logger.warning("storage delete failed: %s", e)
                continue
        await db.files.update_one({"_id": rec["_id"]}, {"$set": {"is_deleted": True, "deleted_at": datetime.now(timezone.utc).isoformat()}})
        removed += 1
    return removed


def design_srcs(d: dict) -> list:
    return [l["src"] for l in d.get("front", []) + d.get("back", []) if l.get("kind") == "image"] + [d.get("thumb"), d.get("mockup")]


@router.post("/upload")
async def upload(file: UploadFile = File(...), public: bool = False, user: dict = Depends(current_user)):
    if public and not await db.creators.find_one({"user_id": user["id"], "status": "approved"}, {"_id": 1}):
        raise HTTPException(403, "Only approved creators can upload product photos")
    # Design artwork must be PNG; approved creators may still use JPEG/WEBP for shop product photos.
    return await store_file(await file.read(), file.content_type or "", user["id"], public, png_only=not public)


@router.post("/admin/upload")
async def admin_upload(request: Request, file: UploadFile = File(...), x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    return await store_file(await file.read(), file.content_type or "", "admin", True)


@router.get("/files/{path:path}")
async def get_file(path: str, user: Optional[dict] = Depends(optional_user), w: Optional[int] = Query(None, ge=1, le=4000)):
    """The image, or with ?w= a WEBP no wider than that (rounded up to 240/480/960; larger = the original)."""
    rec = await db.files.find_one({"path": path, "is_deleted": False}, {"_id": 0})
    allowed = bool(rec) and (rec.get("public") or (user and rec["owner_id"] == user["id"]))
    if rec and not allowed and user and is_admin_phone(user.get("phone")):
        # Admins may open files that belong to an order (artwork they must print), nothing else.
        src = f"/api/files/{path}"
        allowed = bool(await db.orders.find_one(
            {"$or": [{f: src} for f in ("design_snapshot.front.src", "design_snapshot.back.src", "design_snapshot.thumb", "design_snapshot.mockup", "photo",
                                      "design_snapshot.design.front.src", "design_snapshot.design.back.src", "design_snapshot.design.thumb", "design_snapshot.design.mockup")]},
            {"_id": 1},
        ))
    if not allowed:
        raise HTTPException(404, "File not found")
    width = next((t for t in storage.THUMB_WIDTHS if w and w <= t), None)
    public = bool(rec.get("public"))
    cache = "public, max-age=604800" if public else "private, max-age=86400"
    base = storage.public_base()
    if width and rec.get("content_type") != "image/gif":
        if width in rec.get("thumbs", []):
            b, k = _thumb_place(rec, width)
            if public and base and b == storage.bucket(True):
                return RedirectResponse(f"{base}/{k}", 302, headers={"Cache-Control": "public, max-age=86400"})
            try:
                data = await run_in_threadpool(storage.r2_get, b, k)
                return Response(content=data, media_type="image/webp", headers={"Cache-Control": cache})
            except Exception:
                pass  # missing variant: make it again below
        try:
            original = await run_in_threadpool(fetch_bytes, rec)
        except Exception:
            raise HTTPException(502, "Could not load file")
        small = await run_in_threadpool(storage.make_thumb, original, width)
        if small is None:
            return Response(content=original, media_type=rec["content_type"], headers={"Cache-Control": cache})
        if storage.r2_ready():
            try:
                b, k = _thumb_place(rec, width)
                await run_in_threadpool(storage.r2_put, b, k, small, "image/webp", "public, max-age=31536000, immutable" if public else "private, max-age=86400")
                await db.files.update_one({"path": path}, {"$addToSet": {"thumbs": width}})
            except Exception as e:
                logger.info("thumbnail not cached: %s", e)
        return Response(content=small, media_type="image/webp", headers={"Cache-Control": cache})
    if public and base and rec.get("provider") == "r2" and rec.get("bucket") == storage.bucket(True):
        return RedirectResponse(f"{base}/{rec['key']}", 302, headers={"Cache-Control": "public, max-age=86400"})
    try:
        data = await run_in_threadpool(fetch_bytes, rec)
    except Exception:
        raise HTTPException(502, "Could not load file")
    return Response(content=data, media_type=rec["content_type"], headers={"Cache-Control": cache})
