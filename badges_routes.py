"""Badge / vintage templates: built-in layouts rendered server-side into one SVG (shapes + letters as paths).
Admin can switch each template on or off; customers edit the words, font and colours."""
import math
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ai_guard import check_text
from core import check_pin, client_ip, db, optional_user
from fonts_routes import HEX, _font

router = APIRouter(prefix="/api")

# fields: key -> (label, default, max length)
TEMPLATES = {
    "ring": {"name": "COLLEGE RING", "font": "BLOCK", "fields": {"top": ("TOP ARC", "STATE UNIVERSITY", 22), "middle": ("CENTER", "ATHLETICS", 12), "bottom": ("BOTTOM ARC", "EST 1998", 18)}},
    "vintage": {"name": "VINTAGE EST.", "font": "RETRO", "fields": {"top": ("ARCH", "BROOKLYN", 14), "middle": ("MAIN", "SUPPLY CO", 14), "bottom": ("SMALL", "EST. 1998", 16)}},
    "ribbon": {"name": "RIBBON BANNER", "font": "SCRIPT", "fields": {"top": ("ABOVE", "THE ORIGINAL", 20), "middle": ("ON RIBBON", "Good Vibes", 14), "bottom": ("BELOW", "SINCE 2024", 18)}},
    "shield": {"name": "SHIELD CREST", "font": "CONDENSED", "fields": {"top": ("TOP", "FOOTBALL CLUB", 18), "middle": ("MAIN", "LIONS", 10), "bottom": ("BOTTOM", "EST 1987", 14)}},
    "stamp": {"name": "POSTAL STAMP", "font": "TYPEWRITER", "fields": {"top": ("TOP", "OFFICIAL", 18), "middle": ("MAIN", "APPROVED", 12), "bottom": ("BOTTOM", "QUALITY GOODS", 20)}},
    "seal": {"name": "ROSETTE SEAL", "font": "BLOCK", "fields": {"top": ("TOP ARC", "PREMIUM QUALITY", 20), "middle": ("CENTER", "100%", 8), "bottom": ("BOTTOM ARC", "GUARANTEED", 16)}},
    "sunburst": {"name": "RETRO SUNBURST", "font": "ROUNDED", "fields": {"top": ("TOP", "STAY", 14), "middle": ("MAIN", "GOLDEN", 10), "bottom": ("BOTTOM", "SUMMER 2026", 16)}},
    "varsity": {"name": "VARSITY", "font": "BLOCK", "fields": {"top": ("ARCH", "VARSITY", 12), "middle": ("NUMBER", "23", 4)}},
}


class _Ink:
    """Collects letter outlines (in a 1000x1000 badge space) into one SVG path."""

    def __init__(self, font):
        self.gs = font.getGlyphSet()
        self.cmap = font.getBestCmap()
        self.hmtx = font["hmtx"]
        self.upm = font["head"].unitsPerEm
        os2 = font["OS/2"] if "OS/2" in font else None
        self.cap = (getattr(os2, "sCapHeight", 0) or font["hhea"].ascent * 0.7) if os2 else font["hhea"].ascent * 0.7
        self.pen = SVGPathPen(self.gs, ntos=lambda v: f"{v:.1f}".rstrip("0").rstrip("."))
        self.any = False

    def _names(self, text):
        return [self.cmap[ord(c)] for c in text if ord(c) in self.cmap]

    def _draw(self, n, t):
        self.gs[n].draw(TransformPen(self.pen, t))
        self.any = True

    def line(self, text, cx, baseline, size, max_w):
        """Centered straight text; shrinks to fit max_w. `size` = cap height in badge units."""
        names = self._names(text)
        if not names:
            return
        w = sum(self.hmtx[n][0] for n in names)
        k = min(size / self.cap, max_w / w if w else 1)
        x = cx - w * k / 2
        for n in names:
            self._draw(n, (k, 0, 0, -k, x, baseline))
            x += self.hmtx[n][0] * k

    def arc(self, text, cx, cy, radius, size, max_deg, top=True):
        """Text following a circle (top reads over the circle, bottom reads under it), letters' inner edge at `radius`."""
        names = self._names(text)
        if not names:
            return
        track = self.upm * 0.06
        adv = [self.hmtx[n][0] + track for n in names]
        total = sum(adv) - track
        k = size / self.cap
        mid = radius + self.cap * k / 2
        if total * k / mid > math.radians(max_deg):
            k = math.radians(max_deg) * radius / (total - math.radians(max_deg) * self.cap / 2)
            mid = radius + self.cap * k / 2
        base = radius if top else radius + self.cap * k
        s = -total / 2
        for n, a in zip(names, adv):
            w = a - track
            th = (s + w / 2) * k / mid
            phi = th if top else -th
            c, si = math.cos(phi), math.sin(phi)
            px = cx + base * math.sin(th)
            py = cy - base * math.cos(th) if top else cy + base * math.cos(th)
            self._draw(n, (c * k, si * k, si * k, -c * k, px - c * k * w / 2, py - si * k * w / 2))
            s += a

    def d(self):
        return self.pen.getCommands()


def _star(cx, cy, r, inner=0.45, pts=5):
    p = []
    for i in range(pts * 2):
        rr = r if i % 2 == 0 else r * inner
        a = math.pi * i / pts - math.pi / 2
        p.append(f"{cx + rr * math.cos(a):.1f},{cy + rr * math.sin(a):.1f}")
    return f'<polygon points="{" ".join(p)}" fill="{{ink}}"/>'


def _rosette(cx, cy, r1, r2, n=40):
    p = []
    for i in range(n * 2):
        rr = r1 if i % 2 == 0 else r2
        a = math.pi * i / n
        p.append(f"{cx + rr * math.cos(a):.1f},{cy + rr * math.sin(a):.1f}")
    return f'<polygon points="{" ".join(p)}" fill="{{bg}}" stroke="{{ink}}" stroke-width="8" stroke-linejoin="round"/>'


def _rays(cx, cy, r, n=24):
    out = []
    for i in range(n):
        a1, a2 = 2 * math.pi * i / n, 2 * math.pi * (i + 0.5) / n
        out.append(f"M{cx},{cy} L{cx + r * math.cos(a1):.1f},{cy + r * math.sin(a1):.1f} L{cx + r * math.cos(a2):.1f},{cy + r * math.sin(a2):.1f} Z")
    return f'<path d="{" ".join(out)}" fill="{{ink}}" opacity="0.85"/>'


def _layout(tid: str, ink: _Ink, f: dict) -> str:
    """Draws the template's letters into `ink` and returns its shapes (with {ink}/{bg} colour slots)."""
    t, m, b = f.get("top", ""), f.get("middle", ""), f.get("bottom", "")
    if tid == "ring":
        ink.arc(t, 500, 500, 360, 62, 150)
        ink.arc(b, 500, 500, 360, 62, 110, top=False)
        ink.line(m, 500, 555, 110, 540)
        return ('<circle cx="500" cy="500" r="480" fill="{bg}" stroke="{ink}" stroke-width="16"/>'
                '<circle cx="500" cy="500" r="455" fill="none" stroke="{ink}" stroke-width="5"/>'
                '<circle cx="500" cy="500" r="335" fill="none" stroke="{ink}" stroke-width="8"/>'
                + _star(92, 500, 34) + _star(908, 500, 34))
    if tid == "vintage":
        ink.arc(t, 500, 900, 470, 105, 110)
        ink.line(m, 500, 690, 92, 720)
        ink.line(b, 500, 820, 48, 420)
        return ('<rect x="110" y="575" width="780" height="10" fill="{ink}"/><rect x="110" y="725" width="780" height="10" fill="{ink}"/>'
                + _star(200, 800, 26) + _star(800, 800, 26))
    if tid == "ribbon":
        ink.line(t, 500, 380, 56, 620)
        ink.line(m, 500, 548, 92, 660)
        ink.line(b, 500, 690, 46, 520)
        return ('<polygon points="30,470 150,470 150,620 30,620 80,545" fill="{bg}" stroke="{ink}" stroke-width="10" stroke-linejoin="round"/>'
                '<polygon points="970,470 850,470 850,620 970,620 920,545" fill="{bg}" stroke="{ink}" stroke-width="10" stroke-linejoin="round"/>'
                '<rect x="120" y="420" width="760" height="170" fill="{bg}" stroke="{ink}" stroke-width="12"/>')
    if tid == "shield":
        ink.line(t, 500, 270, 52, 560)
        ink.line(m, 500, 590, 190, 560)
        ink.line(b, 500, 735, 50, 380)
        return ('<path d="M500 50 L890 150 L890 470 Q890 790 500 950 Q110 790 110 470 L110 150 Z" fill="{bg}" stroke="{ink}" stroke-width="18" stroke-linejoin="round"/>'
                '<path d="M500 95 L850 185 L850 470 Q850 760 500 905 Q150 760 150 470 L150 185 Z" fill="none" stroke="{ink}" stroke-width="5"/>'
                '<rect x="190" y="320" width="620" height="8" fill="{ink}"/>')
    if tid == "stamp":
        ink.line(t, 500, 380, 50, 640)
        ink.line(m, 500, 575, 130, 700)
        ink.line(b, 500, 690, 44, 640)
        return ('<rect x="70" y="250" width="860" height="500" rx="24" fill="{bg}" stroke="{ink}" stroke-width="16"/>'
                '<rect x="105" y="285" width="790" height="430" rx="12" fill="none" stroke="{ink}" stroke-width="5" stroke-dasharray="22 14"/>')
    if tid == "seal":
        ink.arc(t, 500, 500, 330, 50, 150)
        ink.arc(b, 500, 500, 330, 50, 120, top=False)
        ink.line(m, 500, 560, 120, 420)
        return (_rosette(500, 500, 490, 455) + '<circle cx="500" cy="500" r="420" fill="none" stroke="{ink}" stroke-width="6"/>'
                '<circle cx="500" cy="500" r="310" fill="none" stroke="{ink}" stroke-width="6"/>')
    if tid == "sunburst":
        ink.line(t, 500, 410, 50, 380)
        ink.line(m, 500, 560, 110, 500)
        ink.line(b, 500, 650, 40, 420)
        return _rays(500, 500, 490) + '<circle cx="500" cy="500" r="310" fill="{bg}" stroke="{ink}" stroke-width="14"/>'
    if tid == "varsity":
        ink.arc(t, 500, 1150, 760, 120, 70)
        ink.line(m, 500, 880, 380, 620)
        return ""
    raise HTTPException(404, "Template not found")


def _svg(tid, font, fields, color, accent) -> str:
    ink = _Ink(font)
    shapes = _layout(tid, ink, fields).replace("{ink}", color).replace("{bg}", accent or "none")
    if not ink.any:
        raise HTTPException(400, "This font can't show those characters. Try plain letters or another font.")
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000">{shapes}<path d="{ink.d()}" fill="{color}"/></svg>'


async def _active_map() -> dict:
    return {s["id"]: s["active"] async for s in db.badge_settings.find({}, {"_id": 0})}


async def _default_font(label: str) -> Optional[dict]:
    return await db.fonts.find_one({"label": label, "active": True}, {"_id": 0}) or await db.fonts.find_one({"active": True}, {"_id": 0}, sort=[("order", 1)])


_previews: dict = {}


async def _preview(tid: str) -> tuple:
    f = await _default_font(TEMPLATES[tid]["font"])
    if not f:
        return None, ""
    key = (tid, f["id"])
    if key not in _previews:
        defaults = {k: v[1] for k, v in TEMPLATES[tid]["fields"].items()}
        _previews[key] = await run_in_threadpool(_svg, tid, await _font(f["id"]), defaults, "#111111", "#F2E8D5")
    return f["id"], _previews[key]


def _public(tid, font_id, preview, active=None):
    t = TEMPLATES[tid]
    out = {
        "id": tid, "name": t["name"], "font_id": font_id, "preview": preview,
        "fields": [{"key": k, "label": v[0], "default": v[1], "max": v[2]} for k, v in t["fields"].items()],
    }
    if active is not None:
        out["active"] = active
    return out


@router.get("/badges")
async def list_badges():
    act = await _active_map()
    out = []
    for tid in TEMPLATES:
        if act.get(tid, True):
            fid, prev = await _preview(tid)
            out.append(_public(tid, fid, prev))
    return out


class BadgeIn(BaseModel):
    template: str = Field(max_length=20)
    top: str = Field(default="", max_length=24)
    middle: str = Field(default="", max_length=16)
    bottom: str = Field(default="", max_length=24)
    font_id: str = Field(max_length=40)
    color: str = Field(pattern=HEX)
    accent: Optional[str] = Field(default=None, pattern=HEX)


@router.post("/badges/render")
async def render_badge(body: BadgeIn, request: Request, user: Optional[dict] = Depends(optional_user)):
    from server import ask_claude  # lazy: server imports this module

    t = TEMPLATES.get(body.template)
    if not t or not (await _active_map()).get(body.template, True):
        raise HTTPException(404, "That badge isn't available")
    fields = {k: " ".join(getattr(body, k).split())[: v[2]] for k, v in t["fields"].items()}
    words = " / ".join(v for v in fields.values() if v)
    if not words:
        raise HTTPException(400, "Type some words first")
    if not await db.fonts.find_one({"id": body.font_id, "active": True}, {"_id": 1}):
        raise HTTPException(404, "That font is no longer available")
    await check_text(words, user, client_ip(request), ask_claude)
    font = await _font(body.font_id)
    return {"svg": await run_in_threadpool(_svg, body.template, font, fields, body.color, body.accent)}


class BadgePatch(BaseModel):
    active: bool


@router.get("/admin/badges")
async def admin_badges(request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    act = await _active_map()
    out = []
    for tid in TEMPLATES:
        fid, prev = await _preview(tid)
        out.append(_public(tid, fid, prev, act.get(tid, True)))
    return out


@router.patch("/admin/badges/{tid}")
async def admin_toggle_badge(tid: str, body: BadgePatch, request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    if tid not in TEMPLATES:
        raise HTTPException(404, "Template not found")
    await db.badge_settings.update_one({"id": tid}, {"$set": {"id": tid, "active": body.active}}, upsert=True)
    return {"id": tid, "active": body.active}
