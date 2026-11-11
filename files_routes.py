"""Image uploads to Cloudinary + owner-checked downloads."""
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
from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from core import check_pin, client_ip, current_user, db, optional_user

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


async def store_file(data: bytes, ct: str, owner_id: str, public: bool) -> dict:
    ALLOWED = {"image/png", "image/jpeg", "image/webp", "image/gif"}
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Image is too large (max 8 MB)")
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
    if not cloudinary_ready():
        raise HTTPException(503, "Image storage is not configured")
    try:
        res = await run_in_threadpool(cld_upload, data, owner_id, public)
    except Exception as e:
        logger.warning("cloudinary upload failed: %s", e)
        raise HTTPException(502, "Upload failed, try again")
    path = f"cld/{res['public_id']}.{res['format']}"
    rec.update(path=path, provider="cloudinary", public_id=res["public_id"], format=res["format"],
               delivery_type=res.get("type", "upload"), cloud_url=res["secure_url"])
    await db.files.insert_one(rec)
    return {"path": path, "url": f"/api/files/{path}"}


async def _in_use(src: str) -> bool:
    checks = [
        (db.orders, ["photo", "design_snapshot.front.src", "design_snapshot.back.src", "design_snapshot.thumb", "design_snapshot.photos"]),
        (db.designs, ["front.src", "back.src", "thumb"]),
        (db.catalog_products, ["photos"]),
        (db.shares, ["front_image", "back_image", "design.front.src", "design.back.src", "design.thumb"]),
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
        if rec.get("provider") == "cloudinary" and cloudinary_ready():
            try:
                await run_in_threadpool(
                    lambda: cloudinary.uploader.destroy(rec["public_id"], resource_type="image", type=rec["delivery_type"], invalidate=True)
                )
            except Exception as e:
                logger.warning("cloudinary delete failed: %s", e)
                continue
        await db.files.update_one({"_id": rec["_id"]}, {"$set": {"is_deleted": True, "deleted_at": datetime.now(timezone.utc).isoformat()}})
        removed += 1
    return removed


def design_srcs(d: dict) -> list:
    return [l["src"] for l in d.get("front", []) + d.get("back", []) if l.get("kind") == "image"] + [d.get("thumb")]


@router.post("/upload")
async def upload(file: UploadFile = File(...), public: bool = False, user: dict = Depends(current_user)):
    if public and not await db.creators.find_one({"user_id": user["id"], "status": "approved"}, {"_id": 1}):
        raise HTTPException(403, "Only approved creators can upload product photos")
    return await store_file(await file.read(), file.content_type or "", user["id"], public)


@router.post("/admin/upload")
async def admin_upload(request: Request, file: UploadFile = File(...), x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    return await store_file(await file.read(), file.content_type or "", "admin", True)


@router.get("/files/{path:path}")
async def get_file(path: str, user: Optional[dict] = Depends(optional_user)):
    rec = await db.files.find_one({"path": path, "is_deleted": False}, {"_id": 0})
    if not rec or not (rec.get("public") or (user and rec["owner_id"] == user["id"])):
        raise HTTPException(404, "File not found")
    try:
        cloudinary_ready()
        data = await run_in_threadpool(cld_fetch, rec)
    except requests.RequestException:
        raise HTTPException(502, "Could not load file")
    cache = "public, max-age=86400" if rec.get("public") else "private, max-age=86400"
    return Response(content=data, media_type=rec["content_type"], headers={"Cache-Control": cache})
