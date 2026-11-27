"""Product reviews from verified buyers (a delivered order of that shop product). One review per customer per product
(writing again edits it). Reviews show immediately; admins can hide any. The product keeps its rating average/count
for listings and search results (schema.org AggregateRating)."""
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core import check_pin, client_ip, current_user, db

router = APIRouter(prefix="/api")


class ReviewIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    text: str = Field(default="", max_length=600)


def _public(r: dict) -> dict:
    return {k: r.get(k) for k in ("id", "product_id", "rating", "text", "name", "created_at", "updated_at")}


async def refresh_rating(pid: str):
    agg = await db.reviews.aggregate([
        {"$match": {"product_id": pid, "hidden": {"$ne": True}}},
        {"$group": {"_id": None, "avg": {"$avg": "$rating"}, "n": {"$sum": 1}}},
    ]).to_list(1)
    avg, n = (round(agg[0]["avg"], 2), agg[0]["n"]) if agg else (None, 0)
    await db.catalog_products.update_one({"id": pid}, {"$set": {"rating_avg": avg, "rating_count": n}})


@router.get("/products/{pid}/reviews")
async def list_reviews(pid: str, limit: int = Query(20, ge=1, le=100)):
    docs = await db.reviews.find({"product_id": pid, "hidden": {"$ne": True}}, {"_id": 0}).sort("created_at", -1).to_list(limit)
    p = await db.catalog_products.find_one({"id": pid}, {"_id": 0, "rating_avg": 1, "rating_count": 1}) or {}
    return {"average": p.get("rating_avg"), "count": p.get("rating_count", 0), "reviews": [_public(d) for d in docs]}


@router.post("/products/{pid}/reviews")
async def write_review(pid: str, body: ReviewIn, user: dict = Depends(current_user)):
    if not await db.orders.find_one({"user_id": user["id"], "product_id": pid, "status": "delivered"}, {"_id": 1}):
        raise HTTPException(403, "Only customers who received this product can review it")
    now = datetime.now(timezone.utc).isoformat()
    first = (user.get("name") or "").strip().split(" ")[0][:24]
    existing = await db.reviews.find_one({"product_id": pid, "user_id": user["id"]}, {"_id": 0, "id": 1, "created_at": 1})
    doc = {
        "id": existing["id"] if existing else str(uuid.uuid4()),
        "product_id": pid,
        "user_id": user["id"],
        "rating": body.rating,
        "text": " ".join(body.text.split()),
        "name": f"{first} · Verified buyer" if first else "Verified buyer",
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }
    await db.reviews.update_one({"product_id": pid, "user_id": user["id"]}, {"$set": doc}, upsert=True)
    await refresh_rating(pid)
    return _public(doc)


@router.get("/reviews/mine")
async def my_reviews(user: dict = Depends(current_user)):
    """Product ids (and ratings) this customer has reviewed, so the app knows which orders to offer "Rate it" on."""
    return {r["product_id"]: r["rating"] async for r in db.reviews.find({"user_id": user["id"]}, {"product_id": 1, "rating": 1})}


@router.get("/admin/reviews")
async def admin_reviews(request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    docs = await db.reviews.find({}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(200)
    names = {p["id"]: p["name"] async for p in db.catalog_products.find({"id": {"$in": list({d["product_id"] for d in docs})}}, {"id": 1, "name": 1})}
    return [{**d, "product_name": names.get(d["product_id"], "")} for d in docs]


class HideIn(BaseModel):
    hidden: bool


@router.patch("/admin/reviews/{rid}")
async def hide_review(rid: str, body: HideIn, request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    r = await db.reviews.find_one_and_update({"id": rid}, {"$set": {"hidden": body.hidden}})
    if not r:
        raise HTTPException(404, "Review not found")
    await refresh_rating(r["product_id"])
    return {"id": rid, "hidden": body.hidden}


async def ensure_indexes():
    await db.reviews.create_index([("product_id", 1), ("user_id", 1)], unique=True)
    await db.reviews.create_index([("product_id", 1), ("created_at", -1)])
