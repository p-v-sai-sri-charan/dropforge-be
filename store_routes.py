"""Shop: categories (nested), catalog products with per-size stock, discounts, creators (India KYC) and shares."""
import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from core import check_pin, client_ip, current_user, db
from files_routes import design_srcs, release_files

router = APIRouter(prefix="/api")
now_iso = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731


def admin(request: Request, pin: Optional[str]):
    check_pin(pin, client_ip(request))


# ---------- store settings ----------
class StoreSettings(BaseModel):
    first_order_pct: float = Field(ge=0, le=90)
    commission_pct: float = Field(ge=0, le=90)


async def load_store() -> StoreSettings:
    doc = await db.settings.find_one({"key": "store"}, {"_id": 0, "key": 0})
    return StoreSettings(**doc) if doc else StoreSettings(first_order_pct=10, commission_pct=15)


@router.get("/store/settings")
async def store_settings():
    s = await load_store()
    return {"first_order_pct": s.first_order_pct}


@router.get("/admin/store-settings")
async def admin_store_settings(request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    return await load_store()


@router.put("/admin/store-settings")
async def put_store_settings(body: StoreSettings, request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    await db.settings.update_one({"key": "store"}, {"$set": body.model_dump()}, upsert=True)
    return body


# ---------- discounts ----------
class DiscountIn(BaseModel):
    code: str = Field(min_length=3, max_length=20, pattern=r"^[A-Za-z0-9_-]+$")
    percent: float = Field(ge=1, le=90)
    expires_at: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    active: bool = True


@router.get("/admin/discounts")
async def list_discounts(request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    return await db.discounts.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)


@router.post("/admin/discounts")
async def add_discount(body: DiscountIn, request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    code = body.code.upper()
    if await db.discounts.find_one({"code": code}):
        raise HTTPException(409, "That code already exists")
    doc = {**body.model_dump(), "code": code, "uses": 0, "created_at": now_iso()}
    await db.discounts.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


@router.put("/admin/discounts/{code}")
async def edit_discount(code: str, body: DiscountIn, request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    r = await db.discounts.update_one({"code": code.upper()}, {"$set": {k: v for k, v in body.model_dump().items() if k != "code"}})
    if not r.matched_count:
        raise HTTPException(404, "Code not found")
    return await db.discounts.find_one({"code": code.upper()}, {"_id": 0})


@router.delete("/admin/discounts/{code}")
async def delete_discount(code: str, request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    await db.discounts.delete_one({"code": code.upper()})
    return {"ok": True}


async def find_discount(code: str) -> dict:
    d = await db.discounts.find_one({"code": code.strip().upper()}, {"_id": 0})
    today = datetime.now(timezone.utc).date().isoformat()
    if not d or not d.get("active") or (d.get("expires_at") and d["expires_at"] < today):
        raise HTTPException(400, "This discount code is invalid or expired")
    return d


# ---------- categories ----------
class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    parent_id: Optional[str] = None


@router.get("/catalog/categories")
async def categories():
    return await db.categories.find({}, {"_id": 0}).sort("name", 1).to_list(1000)


@router.post("/admin/categories")
async def add_category(body: CategoryIn, request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    if body.parent_id and not await db.categories.find_one({"id": body.parent_id}):
        raise HTTPException(404, "Parent category not found")
    doc = {"id": str(uuid.uuid4()), "name": body.name.strip(), "parent_id": body.parent_id, "created_at": now_iso()}
    await db.categories.insert_one(dict(doc))
    return doc


@router.put("/admin/categories/{cid}")
async def rename_category(cid: str, body: CategoryIn, request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    r = await db.categories.update_one({"id": cid}, {"$set": {"name": body.name.strip()}})
    if not r.matched_count:
        raise HTTPException(404, "Category not found")
    return await db.categories.find_one({"id": cid}, {"_id": 0})


@router.delete("/admin/categories/{cid}")
async def delete_category(cid: str, request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    if await db.categories.find_one({"parent_id": cid}):
        raise HTTPException(400, "Delete its sub-categories first")
    if await db.catalog_products.find_one({"category_id": cid}):
        raise HTTPException(400, "Move or delete its products first")
    await db.categories.delete_one({"id": cid})
    return {"ok": True}


# ---------- catalog products ----------
class ProductIn(BaseModel):
    category_id: str
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=2000)
    photos: List[str] = Field(default_factory=list, max_length=8)
    prices: Dict[str, float]
    stock: Dict[str, int]
    status: Literal["active", "pending", "hidden"] = "active"
    design_id: Optional[str] = Field(default=None, max_length=60)  # creators: a saved studio design this product is printed from

    @field_validator("photos")
    @classmethod
    def photo_paths(cls, v):
        if any(not p.startswith("/api/files/") for p in v):
            raise ValueError("Photos must be uploaded first")
        return v

    @field_validator("prices")
    @classmethod
    def price_ok(cls, v):
        v = {k: round(float(p), 2) for k, p in v.items() if k in ("INR", "USD", "GBP", "EUR", "AED") and p}
        if not v or any(p < 1 or p > 1_000_000 for p in v.values()):
            raise ValueError("Set a price (1 to 1,000,000) for at least one country; leave 0 to hide the product there")
        return v

    @field_validator("stock")
    @classmethod
    def stock_ok(cls, v):
        v = {k.strip().upper(): int(n) for k, n in v.items() if k.strip()}
        if not 1 <= len(v) <= 12 or any(len(k) > 12 or n < 0 or n > 100000 for k, n in v.items()):
            raise ValueError("Add 1-12 sizes with stock between 0 and 100000")
        return v


async def _with_creators(docs: List[dict]) -> List[dict]:
    for d in docs:  # never expose the artwork layers; the public only sees the preview photos
        dz = d.pop("design", None)
        if dz:
            d["design_id"], d["design_name"] = dz.get("id"), dz.get("name")
    ids = {d["creator_id"] for d in docs if d.get("creator_id")}
    found = {c["id"]: c async for c in db.creators.find({"id": {"$in": list(ids)}}, {"_id": 0, "id": 1, "display_name": 1, "slug": 1})}
    for d in docs:
        c = found.get(d.get("creator_id"))
        d["creator_name"] = c["display_name"] if c else None
        d["creator_slug"] = c.get("slug") if c else None
    return docs


async def _check_category(cid: str):
    if not await db.categories.find_one({"id": cid}):
        raise HTTPException(404, "Category not found")


@router.get("/catalog/products")
async def list_products(
    category_id: Optional[str] = None,
    currency: Optional[Literal["INR", "USD", "GBP", "EUR", "AED"]] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=500),  # pages of e.g. 24 for infinite scroll; default keeps old callers working
    q: Optional[str] = Query(None, max_length=60),  # shop search: every word must appear in the name, description or tags
):
    # With a currency, only products priced in it are shown (no price / 0 = not sold in that region).
    query = {"status": "active", **({"category_id": category_id} if category_id else {}), **({f"prices.{currency}": {"$gt": 0}} if currency else {})}
    words = [w for w in re.split(r"\s+", (q or "").strip()) if w][:6]
    if words:
        query["$and"] = [{"$or": [{f: {"$regex": re.escape(w), "$options": "i"}} for f in ("name", "description", "tags")]} for w in words]
    q = query
    return await _with_creators(await db.catalog_products.find(q, {"_id": 0}).sort([("created_at", -1), ("id", 1)]).skip(skip).limit(limit).to_list(limit))


@router.get("/catalog/products/{pid}")
async def get_product(pid: str, currency: Optional[Literal["INR", "USD", "GBP", "EUR", "AED"]] = None):
    doc = await db.catalog_products.find_one({"id": pid, "status": "active", **({f"prices.{currency}": {"$gt": 0}} if currency else {})}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Product not found")
    return (await _with_creators([doc]))[0]


@router.get("/admin/products")
async def admin_products(request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    return await _with_creators(await db.catalog_products.find({}, {"_id": 0}).sort("created_at", -1).to_list(2000))


@router.post("/admin/products")
async def admin_add_product(body: ProductIn, request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    await _check_category(body.category_id)
    doc = {**body.model_dump(exclude={"design_id"}), "id": str(uuid.uuid4()), "creator_id": None, "created_at": now_iso()}
    await db.catalog_products.insert_one(dict(doc))
    return doc


@router.put("/admin/products/{pid}")
async def admin_edit_product(pid: str, body: ProductIn, request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    await _check_category(body.category_id)
    r = await db.catalog_products.update_one({"id": pid}, {"$set": body.model_dump(exclude={"design_id"})})
    if not r.matched_count:
        raise HTTPException(404, "Product not found")
    return (await _with_creators([await db.catalog_products.find_one({"id": pid}, {"_id": 0})]))[0]


@router.delete("/admin/products/{pid}")
async def admin_delete_product(pid: str, request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    p = await db.catalog_products.find_one({"id": pid}, {"_id": 0, "photos": 1, "design": 1})
    await db.catalog_products.delete_one({"id": pid})
    removed = await release_files((p or {}).get("photos", []) + (design_srcs(p["design"]) if p and p.get("design") else []))
    return {"ok": True, "images_removed": removed}


# ---------- creators (India only, manual KYC review) ----------
class CreatorIn(BaseModel):
    display_name: str = Field(min_length=2, max_length=40)
    legal_name: str = Field(min_length=2, max_length=80)
    pan: str = Field(pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$")
    bank_account: str = Field(pattern=r"^\d{9,18}$")
    ifsc: str = Field(pattern=r"^[A-Z]{4}0[A-Z0-9]{6}$")
    upi: Optional[str] = Field(default=None, pattern=r"^[\w.\-]{2,}@[A-Za-z]{2,}$")
    address: str = Field(min_length=5, max_length=200)
    city: str = Field(min_length=2, max_length=60)
    state: str = Field(min_length=2, max_length=60)
    pincode: str = Field(pattern=r"^\d{6}$")


def _mask(c: dict) -> dict:
    c = dict(c)
    c["bank_account"] = "XXXX" + c["bank_account"][-4:]
    c["pan"] = c["pan"][:2] + "XXXXX" + c["pan"][-3:]
    return c


async def _creator(user: dict, approved: bool = False) -> dict:
    c = await db.creators.find_one({"user_id": user["id"]}, {"_id": 0})
    if not c:
        raise HTTPException(404, "Not registered as a creator")
    if approved and c["status"] != "approved":
        raise HTTPException(403, "Your creator account is not approved yet")
    return c


@router.post("/creators/apply")
async def creator_apply(body: CreatorIn, user: dict = Depends(current_user)):
    from auth_routes import _is_test

    if not (user["phone"].startswith("+91") or _is_test(user["phone"])):
        raise HTTPException(400, "The creator program is only for Indian phone numbers (+91)")
    existing = await db.creators.find_one({"user_id": user["id"]}, {"_id": 0})
    if existing and existing["status"] == "approved":
        raise HTTPException(400, "You are already an approved creator")
    doc = {
        **body.model_dump(),
        "id": existing["id"] if existing else str(uuid.uuid4()),
        "user_id": user["id"],
        "phone": user["phone"],
        "status": "pending",
        "note": None,
        "created_at": existing["created_at"] if existing else now_iso(),
    }
    await db.creators.update_one({"user_id": user["id"]}, {"$set": doc}, upsert=True)
    return _mask(doc)


@router.get("/creators/me")
async def creator_me(user: dict = Depends(current_user)):
    c = await db.creators.find_one({"user_id": user["id"]}, {"_id": 0})
    if not c:
        return {"creator": None}
    if c["status"] == "approved":
        c = await _ensure_slug(c)
    earn = await db.earnings.find({"creator_id": c["id"]}, {"_id": 0}).sort("created_at", -1).to_list(500)
    store = await load_store()
    return {"creator": _mask(c), "earnings": earn, "commission_pct": store.commission_pct}


@router.get("/creators/products")
async def creator_products(user: dict = Depends(current_user)):
    c = await _creator(user)
    return await _with_creators(await db.catalog_products.find({"creator_id": c["id"]}, {"_id": 0}).sort("created_at", -1).to_list(500))


async def _link_design(user: dict, design_id: str) -> dict:
    """Snapshot of one of the creator's saved studio designs. Orders for the product carry this snapshot so the admin gets the
    exact artwork placement. Only the preview images are made public; the artwork files stay private."""
    d = await db.designs.find_one({"id": design_id, "user_id": user["id"]}, {"_id": 0, "user_id": 0})
    if not d:
        raise HTTPException(404, "Design not found. Save it in the studio first")
    if not (d.get("front") or d.get("back")):
        raise HTTPException(400, "That design is empty")
    previews = [p for p in (d.get("thumb"), d.get("mockup")) if p]
    await db.files.update_many({"path": {"$in": [p[len("/api/files/"):] for p in previews]}, "owner_id": user["id"]}, {"$set": {"public": True}})
    return {k: d.get(k) for k in ("id", "name", "product", "color", "front", "back", "thumb", "mockup")}


@router.post("/creators/products")
async def creator_add_product(body: ProductIn, user: dict = Depends(current_user)):
    c = await _creator(user, approved=True)
    await _check_category(body.category_id)
    if not body.design_id:
        raise HTTPException(400, "Attach a design made in the studio so we can print your product exactly")
    design = await _link_design(user, body.design_id)
    doc = {**body.model_dump(exclude={"design_id"}), "status": "pending", "id": str(uuid.uuid4()), "creator_id": c["id"], "created_at": now_iso(), "design": design}
    if not doc["photos"]:
        doc["photos"] = [p for p in (design.get("thumb"), design.get("mockup")) if p]
    await db.catalog_products.insert_one(dict(doc))
    return (await _with_creators([doc]))[0]


@router.put("/creators/products/{pid}")
async def creator_edit_product(pid: str, body: ProductIn, user: dict = Depends(current_user)):
    c = await _creator(user, approved=True)
    await _check_category(body.category_id)
    old = await db.catalog_products.find_one({"id": pid, "creator_id": c["id"]}, {"_id": 0})
    if not old:
        raise HTTPException(404, "Product not found")
    if not body.design_id and not old.get("design"):
        raise HTTPException(400, "Attach a design made in the studio so we can print your product exactly")
    patch = {**body.model_dump(exclude={"design_id"}), "status": "pending"}
    design = old.get("design")
    if body.design_id:
        design = await _link_design(user, body.design_id)  # re-attaching the same design picks up its latest edits
    if not patch["photos"] and design:
        patch["photos"] = [p for p in (design.get("thumb"), design.get("mockup")) if p]
    patch["design"] = design
    await db.catalog_products.update_one({"id": pid, "creator_id": c["id"]}, {"$set": patch})
    gone = [p for p in old.get("photos", []) if p not in patch["photos"]]
    if old.get("design") and design and old["design"] != design:
        gone += design_srcs(old["design"])
    if gone:
        await release_files(gone)
    return (await _with_creators([await db.catalog_products.find_one({"id": pid}, {"_id": 0})]))[0]


@router.delete("/creators/products/{pid}")
async def creator_delete_product(pid: str, user: dict = Depends(current_user)):
    c = await _creator(user)
    p = await db.catalog_products.find_one({"id": pid, "creator_id": c["id"]}, {"_id": 0, "photos": 1, "design": 1})
    if p:
        await db.catalog_products.delete_one({"id": pid, "creator_id": c["id"]})
        await release_files(p.get("photos", []) + (design_srcs(p["design"]) if p.get("design") else []))
    return {"ok": True}


# ---------- creator storefronts ("sub stores") ----------
RESERVED_SLUGS = {"admin", "api", "creator", "shop", "login", "studio", "checkout", "manage", "store", "stores", "orders", "product", "www", "dropforge"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,29}$")


async def _ensure_slug(c: dict) -> dict:
    if c.get("slug"):
        return c
    base = re.sub(r"[^a-z0-9]+", "-", c["display_name"].lower()).strip("-")[:24]
    if len(base) < 3:
        base = f"{base}-store".strip("-")
    slug, n = base, 1
    while slug in RESERVED_SLUGS or await db.creators.find_one({"slug": slug}, {"_id": 1}):
        n += 1
        slug = f"{base}-{n}"
    await db.creators.update_one({"id": c["id"]}, {"$set": {"slug": slug}})
    return {**c, "slug": slug}


class StoreIn(BaseModel):
    slug: str = Field(min_length=3, max_length=30)
    bio: str = Field(default="", max_length=300)


@router.put("/creators/me/store")
async def creator_set_store(body: StoreIn, user: dict = Depends(current_user)):
    c = await _creator(user, approved=True)
    slug = body.slug.strip().lower()
    if not SLUG_RE.match(slug) or slug in RESERVED_SLUGS:
        raise HTTPException(400, "Store link: 3-30 letters, numbers or dashes, and not a reserved word")
    if await db.creators.find_one({"slug": slug, "id": {"$ne": c["id"]}}, {"_id": 1}):
        raise HTTPException(409, "That store link is taken")
    await db.creators.update_one({"id": c["id"]}, {"$set": {"slug": slug, "bio": body.bio.strip()}})
    return {"slug": slug, "bio": body.bio.strip()}


@router.get("/stores/{slug}")
async def creator_store(slug: str, currency: Optional[Literal["INR", "USD", "GBP", "EUR", "AED"]] = None):
    """Public storefront: one creator's live products."""
    c = await db.creators.find_one({"slug": slug.lower(), "status": "approved"}, {"_id": 0})
    if not c:
        raise HTTPException(404, "Store not found")
    q = {"creator_id": c["id"], "status": "active", **({f"prices.{currency}": {"$gt": 0}} if currency else {})}
    products = await _with_creators(await db.catalog_products.find(q, {"_id": 0}).sort("created_at", -1).to_list(200))
    return {"creator": {"display_name": c["display_name"], "slug": c["slug"], "bio": c.get("bio", "")}, "products": products}


class CreatorReview(BaseModel):
    status: Literal["approved", "rejected", "pending"]
    note: Optional[str] = Field(default=None, max_length=300)


@router.get("/admin/creators")
async def admin_creators(request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    creators = await db.creators.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    earn = await db.earnings.find({}, {"_id": 0}).sort("created_at", -1).to_list(5000)
    for c in creators:
        c["earnings"] = [e for e in earn if e["creator_id"] == c["id"]]
    return creators


@router.patch("/admin/creators/{cid}")
async def review_creator(cid: str, body: CreatorReview, request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    r = await db.creators.update_one({"id": cid}, {"$set": body.model_dump()})
    if not r.matched_count:
        raise HTTPException(404, "Creator not found")
    return await db.creators.find_one({"id": cid}, {"_id": 0})


@router.patch("/admin/earnings/{eid}")
async def mark_earning(eid: str, request: Request, x_admin_pin: Optional[str] = Header(None)):
    admin(request, x_admin_pin)
    r = await db.earnings.update_one({"id": eid, "status": "pending"}, {"$set": {"status": "paid", "paid_at": now_iso()}})
    if not r.matched_count:
        raise HTTPException(404, "Unpaid earning not found")
    return await db.earnings.find_one({"id": eid}, {"_id": 0})


# ---------- shares ----------
class ShareIn(BaseModel):
    design_id: str
    front_image: Optional[str] = Field(default=None, pattern=r"^/api/files/")
    back_image: Optional[str] = Field(default=None, pattern=r"^/api/files/")


def base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    return f"{proto}://{host}"


@router.post("/shares")
async def create_share(body: ShareIn, request: Request, user: dict = Depends(current_user)):
    d = await db.designs.find_one({"id": body.design_id, "user_id": user["id"]}, {"_id": 0, "user_id": 0})
    if not d:
        raise HTTPException(404, "Save the design to your account first")
    paths = [l["src"][len("/api/files/"):] for l in d["front"] + d["back"] if l["kind"] == "image"]
    paths += [p[len("/api/files/"):] for p in (body.front_image, body.back_image) if p]
    await db.files.update_many({"path": {"$in": paths}, "owner_id": user["id"]}, {"$set": {"public": True}})
    slug = re.sub(r"[^a-z0-9]+", "-", d["name"].lower()).strip("-")[:24] + "-" + secrets.token_urlsafe(5).lower().replace("_", "")
    doc = {"slug": slug, "user_id": user["id"], "design": d, "front_image": body.front_image, "back_image": body.back_image, "created_at": now_iso()}
    await db.shares.insert_one(dict(doc))
    return {"slug": slug, "url": f"{base_url(request)}/api/s/{slug}"}


@router.get("/shares/{slug}")
async def get_share(slug: str):
    sh = await db.shares.find_one({"slug": slug}, {"_id": 0, "user_id": 0})
    if not sh:
        raise HTTPException(404, "Link not found")
    return sh
