"""First-party campaign tracking for promotions: where visitors come from (UTM tags / referrer) and the key shopping
steps, plus the admin "campaigns" report. No third-party scripts; visitors are anonymous ids; events expire."""
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from core import check_pin, client_ip, db, optional_user

router = APIRouter(prefix="/api")

EVENT_DAYS = 400  # raw events are kept ~13 months (TTL index), orders keep their attribution forever


class Attribution(BaseModel):
    """Where a visitor came from: the last tagged / referred visit within 30 days (set by the app / website)."""
    source: Optional[str] = Field(default=None, max_length=100)
    medium: Optional[str] = Field(default=None, max_length=100)
    campaign: Optional[str] = Field(default=None, max_length=100)
    content: Optional[str] = Field(default=None, max_length=100)
    term: Optional[str] = Field(default=None, max_length=100)
    referrer: Optional[str] = Field(default=None, max_length=300)
    landing: Optional[str] = Field(default=None, max_length=300)
    at: Optional[str] = Field(default=None, max_length=40)

    @field_validator("source", "medium", "campaign")
    @classmethod
    def norm(cls, v):  # "Instagram" and "instagram " are the same source in the report
        v = (v or "").strip().lower()
        return v or None

    def clean(self) -> Optional[dict]:
        d = {k: v for k, v in self.model_dump().items() if v}
        return d or None


EventName = Literal["visit", "product_view", "design_started", "checkout_started"]


class EventIn(BaseModel):
    event: EventName  # purchases are recorded from the orders themselves, never from the client
    anon_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    platform: Literal["web", "ios", "android"]
    path: Optional[str] = Field(default=None, max_length=300)
    product_id: Optional[str] = Field(default=None, max_length=64)
    attribution: Optional[Attribution] = None


@router.post("/track", status_code=204)
async def track(body: EventIn, user: Optional[dict] = Depends(optional_user)):
    attr = body.attribution.clean() if body.attribution else None
    await db.events.insert_one({
        "event": body.event,
        "anon_id": body.anon_id,
        "user_id": user["id"] if user else None,
        "platform": body.platform,
        "path": body.path,
        "product_id": body.product_id,
        "source": (attr or {}).get("source"),
        "medium": (attr or {}).get("medium"),
        "campaign": (attr or {}).get("campaign"),
        "at": datetime.now(timezone.utc),  # a real date, for the TTL index
    })
    if user and attr:  # first campaign that brought this customer, kept on their profile
        await db.users.update_one({"id": user["id"], "attribution": {"$exists": False}}, {"$set": {"attribution": attr}})
    return Response(status_code=204)


async def ensure_indexes():
    await db.events.create_index("at", expireAfterSeconds=EVENT_DAYS * 86400)
    await db.events.create_index([("source", 1), ("campaign", 1), ("event", 1)])


PAID = ["paid", "printed", "shipped", "delivered"]
FUNNEL = {"product_view": "product_views", "design_started": "design_started", "checkout_started": "checkout_started"}  # event -> report column
DIRECT = "(direct)"


@router.get("/admin/marketing/campaigns")
async def campaigns(request: Request, days: int = Query(30, ge=1, le=365), x_admin_pin: Optional[str] = Header(None)):
    """Per source + campaign: unique visitors, funnel steps, paid orders and revenue (minor units, per currency)."""
    check_pin(x_admin_pin, client_ip(request))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows: dict = {}

    def row(src, camp):
        key = (src or DIRECT, camp or "")
        if key not in rows:
            rows[key] = {"source": key[0], "campaign": key[1], "visitors": 0, "product_views": 0, "design_started": 0, "checkout_started": 0,
                         "orders": 0, "revenue": {}}
        return rows[key]

    events = db.events.aggregate([
        {"$match": {"at": {"$gte": since}}},
        {"$group": {"_id": {"s": "$source", "c": "$campaign", "e": "$event"}, "n": {"$sum": 1}, "who": {"$addToSet": "$anon_id"}}},
    ])
    async for g in events:
        r = row(g["_id"].get("s"), g["_id"].get("c"))
        e = g["_id"]["e"]
        if e == "visit":
            r["visitors"] += len(g["who"])
        elif e in FUNNEL:
            r[FUNNEL[e]] += g["n"]

    orders = db.orders.aggregate([
        {"$match": {"status": {"$in": PAID}, "paid_at": {"$gte": since.isoformat()}}},
        {"$group": {"_id": {"s": "$attribution.source", "c": "$attribution.campaign", "cur": "$currency"}, "n": {"$sum": 1}, "amt": {"$sum": "$amount"}}},
    ])
    async for g in orders:
        r = row(g["_id"].get("s"), g["_id"].get("c"))
        r["orders"] += g["n"]
        cur = g["_id"]["cur"]
        r["revenue"][cur] = r["revenue"].get(cur, 0) + g["amt"]

    out = sorted(rows.values(), key=lambda r: (-r["orders"], -r["visitors"], r["source"], r["campaign"]))
    return {"days": days, "since": since.isoformat(), "rows": out}
