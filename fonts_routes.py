"""Admin-controlled fonts for text layers.

Fonts are Google Fonts TTFs stored in Cloudinary (raw). Text is rendered server-side into
SVG paths, so text layers look identical everywhere (studio, 3D hoodie, mockups, share page, print).
"""
import asyncio
import io
import logging
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, Literal, Optional

import cloudinary.uploader
import requests
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from ai_guard import check_text
from core import check_pin, client_ip, db, optional_user
from files_routes import cloudinary_ready

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)

DEFAULT_FONTS = [
    ("Anton", "BLOCK"),
    ("Pacifico", "SCRIPT"),
    ("Bungee", "RETRO"),
    ("Special Elite", "TYPEWRITER"),
    ("Bebas Neue", "CONDENSED"),
    ("Lilita One", "ROUNDED"),
]
HEX = r"^#[0-9A-Fa-f]{6}$"
_cache: Dict[str, TTFont] = {}


def _download_google_ttf(family: str) -> bytes:
    css = requests.get("https://fonts.googleapis.com/css2", params={"family": family}, headers={"User-Agent": "Wget/1.0"}, timeout=20)
    m = re.search(r"url\((https://fonts\.gstatic\.com/[^)]+\.ttf)\)", css.text) if css.status_code == 200 else None
    if not m:
        raise HTTPException(404, f"Google Fonts has no font called '{family}'")
    r = requests.get(m.group(1), timeout=30)
    r.raise_for_status()
    return r.content


CURVES = {"arc": 1, "smile": -1, "circle": 1}
BEND_DEG = {1: 70, 2: 130, 3: 200}


def _render(font: TTFont, text: str, color: str, outline: Optional[str], curve: str = "none", bend: int = 2) -> str:
    gs = font.getGlyphSet()
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    upm = font["head"].unitsPerEm
    hhea = font["hhea"]
    line_h = (hhea.ascent - hhea.descent) * 1.05
    pen = SVGPathPen(gs, ntos=lambda v: str(round(v)))
    bounds = BoundsPen(gs)

    def draw(n, t):
        gs[n].draw(TransformPen(pen, t))
        gs[n].draw(TransformPen(bounds, t))

    if curve in CURVES:
        # Letters sit on a circle: each glyph is placed at its arc position and rotated to follow it.
        names = [cmap[ord(ch)] for ch in " ".join(text.split("\n")) if ord(ch) in cmap]
        if curve == "circle" and len(names) < 8:
            curve, bend = "arc", 3  # too few letters to wrap a full circle nicely
        track = upm * 0.03
        adv = [hmtx[n][0] + track for n in names]
        total = sum(adv)
        sweep = math.radians(340 if curve == "circle" else BEND_DEG.get(bend, 130))
        r = total / sweep
        sign = CURVES[curve]
        # Smile letters point toward the centre: space them along their tops so they don't crowd together.
        rb = r + (hhea.ascent * 0.38 if sign < 0 else 0)
        s = -total / 2
        for n, a in zip(names, adv):
            th = (s + a / 2) / r
            c, si = math.cos(th), math.sin(th) * sign
            px, py = rb * math.sin(th), sign * (rb - rb * math.cos(th))
            w = a - track
            draw(n, (c, si, si, -c, px - c * w / 2, py - si * w / 2))
            s += a
    else:
        for i, line in enumerate(text.split("\n")[:3]):
            names = [cmap[ord(ch)] for ch in line if ord(ch) in cmap]
            x = -sum(hmtx[n][0] for n in names) / 2
            y = i * line_h
            for n in names:
                draw(n, (1, 0, 0, -1, x, y))
                x += hmtx[n][0]
    b = bounds.bounds
    if not b:
        raise HTTPException(400, "This font can't show those characters. Try plain letters or another font.")
    d = pen.getCommands()
    sw = round(upm * 0.05) if outline else 0
    pad = sw + upm * 0.02
    vb = f"{b[0] - pad:.0f} {b[1] - pad:.0f} {b[2] - b[0] + 2 * pad:.0f} {b[3] - b[1] + 2 * pad:.0f}"
    back = f'<path d="{d}" fill="{outline}" stroke="{outline}" stroke-width="{sw * 2}" stroke-linejoin="round"/>' if outline else ""
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}">{back}<path d="{d}" fill="{color}"/></svg>'


async def _font(font_id: str) -> TTFont:
    if font_id in _cache:
        return _cache[font_id]
    rec = await db.fonts.find_one({"id": font_id}, {"_id": 0})
    if not rec:
        raise HTTPException(404, "Font not found")

    def load():
        r = requests.get(rec["url"], timeout=30)
        r.raise_for_status()
        return TTFont(io.BytesIO(r.content))

    _cache[font_id] = await run_in_threadpool(load)
    return _cache[font_id]


async def _add_font(family: str, label: str) -> dict:
    family = family.strip()
    if await db.fonts.find_one({"family": {"$regex": f"^{re.escape(family)}$", "$options": "i"}}):
        raise HTTPException(409, "That font is already added")
    if not cloudinary_ready():
        raise HTTPException(503, "Image storage isn't set up yet")
    data = await run_in_threadpool(_download_google_ttf, family)
    font = TTFont(io.BytesIO(data))
    fid = str(uuid.uuid4())
    res = await run_in_threadpool(
        lambda: cloudinary.uploader.upload(io.BytesIO(data), resource_type="raw", folder="dropforge/fonts", public_id=f"{fid}.ttf")
    )
    _cache[fid] = font
    count = await db.fonts.count_documents({})
    doc = {
        "id": fid, "family": family, "label": label.strip().upper()[:20], "url": res["secure_url"], "public_id": res["public_id"],
        "active": True, "order": count, "preview": _render(font, label.strip().upper()[:20] or family, "#111111", None),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.fonts.insert_one(dict(doc))
    return doc


async def seed_fonts():
    """Adds any default font that's missing (retries ones that failed earlier). Fonts the admin deleted stay deleted."""
    if not cloudinary_ready():
        return
    for family, label in DEFAULT_FONTS:
        if await db.fonts.find_one({"family": family}) or await db.font_removed.find_one({"family": family}):
            continue
        try:
            await _add_font(family, label)
        except Exception as e:
            logger.warning("font seed %s failed: %s", family, e)


def start_seed():
    asyncio.get_event_loop().create_task(seed_fonts())


# ---------- public ----------
@router.get("/fonts")
async def list_fonts():
    return await db.fonts.find({"active": True}, {"_id": 0, "id": 1, "label": 1, "family": 1, "preview": 1}).sort("order", 1).to_list(100)


class TextIn(BaseModel):
    text: str = Field(min_length=1, max_length=60)
    font_id: str
    color: str = Field(pattern=HEX)
    outline: Optional[str] = Field(default=None, pattern=HEX)
    curve: Literal["none", "arc", "smile", "circle"] = "none"
    bend: int = Field(default=2, ge=1, le=3)


@router.post("/text/render")
async def render_text(body: TextIn, request: Request, user: Optional[dict] = Depends(optional_user)):
    from server import ask_claude  # lazy: server imports this module

    text = "\n".join(l.strip() for l in body.text.strip().splitlines() if l.strip())
    if not text:
        raise HTTPException(400, "Type some letters first")
    rec = await db.fonts.find_one({"id": body.font_id, "active": True}, {"_id": 1})
    if not rec:
        raise HTTPException(404, "That font is no longer available")
    await check_text(text, user, client_ip(request), ask_claude)
    font = await _font(body.font_id)
    return {"svg": await run_in_threadpool(_render, font, text, body.color, body.outline, body.curve, body.bend)}


# ---------- admin ----------
class FontIn(BaseModel):
    family: str = Field(min_length=2, max_length=60, pattern=r"^[A-Za-z0-9 ]+$")
    label: str = Field(min_length=1, max_length=20)

    @field_validator("family", "label")
    @classmethod
    def strip_text(cls, v):
        if not v.strip():
            raise ValueError("Can't be blank")
        return v.strip()


class FontPatch(BaseModel):
    active: Optional[bool] = None
    label: Optional[str] = Field(default=None, min_length=1, max_length=20)

    @field_validator("label")
    @classmethod
    def strip_label(cls, v):
        if v is not None and not v.strip():
            raise ValueError("Can't be blank")
        return v.strip() if v else v


@router.get("/admin/fonts")
async def admin_fonts(request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    return await db.fonts.find({}, {"_id": 0, "url": 0, "public_id": 0}).sort("order", 1).to_list(200)


@router.post("/admin/fonts")
async def admin_add_font(body: FontIn, request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    doc = await _add_font(body.family, body.label)
    return {k: v for k, v in doc.items() if k not in ("url", "public_id")}


@router.patch("/admin/fonts/{fid}")
async def admin_edit_font(fid: str, body: FontPatch, request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if "label" in patch:
        patch["label"] = patch["label"].strip().upper()
        patch["preview"] = _render(await _font(fid), patch["label"], "#111111", None)
    r = await db.fonts.update_one({"id": fid}, {"$set": patch})
    if not r.matched_count:
        raise HTTPException(404, "Font not found")
    return await db.fonts.find_one({"id": fid}, {"_id": 0, "url": 0, "public_id": 0})


@router.delete("/admin/fonts/{fid}")
async def admin_delete_font(fid: str, request: Request, x_admin_pin: Optional[str] = Header(None)):
    """Existing text layers keep working: their letters are already saved as shapes."""
    check_pin(x_admin_pin, client_ip(request))
    rec = await db.fonts.find_one({"id": fid})
    if not rec:
        raise HTTPException(404, "Font not found")
    await db.fonts.delete_one({"id": fid})
    await db.font_removed.update_one({"family": rec["family"]}, {"$set": {"family": rec["family"]}}, upsert=True)
    _cache.pop(fid, None)
    try:
        if cloudinary_ready():
            await run_in_threadpool(cloudinary.uploader.destroy, rec["public_id"], resource_type="raw", invalidate=True)
    except Exception as e:
        logger.warning("font file delete failed: %s", e)
    return {"ok": True}
