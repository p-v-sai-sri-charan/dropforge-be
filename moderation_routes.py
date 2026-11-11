"""Admin moderation: remove customer designs and share links, review blocked AI requests."""
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request

from core import check_pin, client_ip, db
from files_routes import design_srcs, release_files

router = APIRouter(prefix="/api/admin")


async def _phones(user_ids) -> dict:
    return {u["id"]: u.get("phone") async for u in db.users.find({"id": {"$in": list(set(user_ids))}}, {"_id": 0, "id": 1, "phone": 1})}


@router.get("/designs")
async def all_designs(request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    docs = await db.designs.find({}, {"_id": 0}).sort("updatedAt", -1).to_list(300)
    phones = await _phones(d.get("user_id") for d in docs)
    return [{**d, "phone": phones.get(d.get("user_id"))} for d in docs]


@router.delete("/designs/{design_id}")
async def remove_design(design_id: str, request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    d = await db.designs.find_one({"id": design_id}, {"_id": 0})
    if not d:
        raise HTTPException(404, "Design not found")
    await db.designs.delete_one({"id": design_id})
    return {"ok": True, "images_removed": await release_files(design_srcs(d))}


@router.get("/shares")
async def all_shares(request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    docs = await db.shares.find({}, {"_id": 0}).sort("created_at", -1).to_list(300)
    phones = await _phones(s.get("user_id") for s in docs)
    return [{**s, "phone": phones.get(s.get("user_id"))} for s in docs]


@router.delete("/shares/{slug}")
async def remove_share(slug: str, request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    sh = await db.shares.find_one({"slug": slug}, {"_id": 0})
    if not sh:
        raise HTTPException(404, "Link not found")
    await db.shares.delete_one({"slug": slug})
    return {"ok": True, "images_removed": await release_files(design_srcs(sh.get("design") or {}) + [sh.get("front_image"), sh.get("back_image")])}


@router.get("/ai-blocks")
async def ai_blocks(request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    return await db.ai_violations.find({}, {"_id": 0}).sort("created_at", -1).to_list(300)


@router.delete("/ai-blocks")
async def ai_unlock(key: str, request: Request, x_admin_pin: Optional[str] = Header(None)):
    """Clears a person's strikes so they can use AI again."""
    check_pin(x_admin_pin, client_ip(request))
    r = await db.ai_violations.delete_many({"key": key})
    return {"ok": True, "cleared": r.deleted_count}
