"""Cloud designs: one document holds front + back for a product."""
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from core import current_user, db
from svg_sanitize import sanitize_svg
from files_routes import design_srcs, release_files

router = APIRouter(prefix="/api")


class TextMeta(BaseModel):
    value: str = Field(min_length=1, max_length=60)
    font_id: str = Field(max_length=40)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    outline: Optional[str] = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    curve: Literal["none", "arc", "smile", "circle"] = "none"
    bend: int = Field(default=2, ge=1, le=3)


class BadgeMeta(BaseModel):
    template: str = Field(max_length=20)
    top: str = Field(default="", max_length=24)
    middle: str = Field(default="", max_length=16)
    bottom: str = Field(default="", max_length=24)
    font_id: str = Field(max_length=40)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    accent: Optional[str] = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


class LayerM(BaseModel):
    id: str = Field(max_length=40)
    kind: Literal["image", "svg", "text", "badge"]
    src: str = Field(max_length=300000)
    x: float
    y: float
    scale: float
    rotation: float
    text: Optional[TextMeta] = None
    badge: Optional[BadgeMeta] = None

    @field_validator("src")
    @classmethod
    def no_inline_images(cls, v, info):
        kind = info.data.get("kind")
        if kind == "image":
            if not v.startswith("/api/files/"):
                raise ValueError("Images must be uploaded before saving")
            return v
        if not v.lstrip().startswith("<svg") or len(v) > (300000 if kind in ("text", "badge") else 60000):
            raise ValueError("Invalid graphic")
        # Untrusted input (nothing stops a client from PUTting a layer without going through our generators):
        # strip to plain vector drawing so a saved layer can never carry a script or an external reference that
        # "phones home" whenever it is next rendered - including in the admin's own production-sheet view.
        try:
            return sanitize_svg(v)
        except ValueError:
            raise ValueError("Invalid graphic")


class DesignIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    product: Literal["tee", "hoodie", "tote", "tee3d", "sleeveless", "sweatshirt", "croptop"] = "tee"
    color: str = Field(max_length=12)
    front: List[LayerM] = Field(max_length=20)
    back: List[LayerM] = Field(max_length=20)
    thumb: Optional[str] = Field(default=None, max_length=300)
    mockup: Optional[str] = Field(default=None, max_length=300)  # final front + back mockup PNG (used as the print reference on orders)


@router.get("/designs")
async def list_designs(user: dict = Depends(current_user)):
    return await db.designs.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("updatedAt", -1).to_list(500)


@router.get("/designs/{design_id}")
async def get_design(design_id: str, user: dict = Depends(current_user)):
    doc = await db.designs.find_one({"id": design_id, "user_id": user["id"]}, {"_id": 0, "user_id": 0})
    if not doc:
        raise HTTPException(404, "Design not found")
    return doc


@router.put("/designs/{design_id}")
async def put_design(design_id: str, body: DesignIn, user: dict = Depends(current_user)):
    other = await db.designs.find_one({"id": design_id, "user_id": {"$ne": user["id"]}}, {"_id": 1})
    if other:
        raise HTTPException(409, "Design id already in use")
    old = await db.designs.find_one({"id": design_id, "user_id": user["id"]}, {"_id": 0, "thumb": 1, "mockup": 1})
    doc = {**body.model_dump(), "id": design_id, "updatedAt": datetime.now(timezone.utc).isoformat()}
    await db.designs.update_one({"id": design_id, "user_id": user["id"]}, {"$set": {**doc, "user_id": user["id"]}}, upsert=True)
    if old:
        replaced = [old[k] for k in ("thumb", "mockup") if old.get(k) and old[k] != doc.get(k)]
        if replaced:
            await release_files(replaced)  # replaced previews; kept if an order or share still uses them
    return doc


@router.delete("/designs/{design_id}")
async def delete_design(design_id: str, user: dict = Depends(current_user)):
    d = await db.designs.find_one({"id": design_id, "user_id": user["id"]}, {"_id": 0})
    if d:
        await db.designs.delete_one({"id": design_id, "user_id": user["id"]})
        await release_files(design_srcs(d))
    return {"ok": True}
