"""Admin dashboard numbers: sales, orders, customers and traffic for a period, compared with the period before it,
plus daily series for charts. Days are counted in India time. Revenue = paid orders (paid, printed, shipped,
delivered), in minor units per currency; charts use INR."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Header, Query, Request

from core import check_pin, client_ip, db

router = APIRouter(prefix="/api")

IST = timezone(timedelta(hours=5, minutes=30))
PAID = {"paid", "printed", "shipped", "delivered"}
TYPE_LABEL = {"catalog": "Shop products", "tee": "Custom tees", "hoodie": "Custom hoodies", "sweatshirt": "Custom sweatshirts", "tote": "Custom totes"}
MAX_DOCS = 200_000  # safety cap per query


def _day(v) -> str:
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(IST).date().isoformat()


def _pct(cur: float, prev: float) -> Optional[float]:
    return None if not prev else round((cur - prev) / prev * 100, 1)


@router.get("/admin/analytics")
async def analytics(request: Request, days: int = Query(30, ge=7, le=365), x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    now = datetime.now(timezone.utc)
    today = now.astimezone(IST).date()
    first = today - timedelta(days=days - 1)
    start = datetime.combine(first, datetime.min.time(), IST).astimezone(timezone.utc)
    prev_start = start - timedelta(days=days)
    dates = [(first + timedelta(days=i)).isoformat() for i in range(days)]

    # ---- orders (this period + the one before, for comparison)
    orders = await db.orders.find(
        {"created_at": {"$gte": prev_start.isoformat()}, "status": {"$ne": "created"}},
        {"_id": 0, "id": 1, "status": 1, "amount": 1, "currency": 1, "created_at": 1, "product": 1, "product_id": 1, "design_name": 1, "qty": 1,
         "user_id": 1, "attribution": 1, "discount_label": 1},
    ).to_list(MAX_DOCS)
    cur = [o for o in orders if o["created_at"] >= start.isoformat()]
    prev = [o for o in orders if o["created_at"] < start.isoformat()]
    paid_cur = [o for o in cur if o["status"] in PAID]
    paid_prev = [o for o in prev if o["status"] in PAID]

    def revenue(lst):
        out = Counter()
        for o in lst:
            out[o["currency"]] += o["amount"]
        return dict(out)

    inr = lambda lst: sum(o["amount"] for o in lst if o["currency"] == "INR")
    series = {d: {"date": d, "revenue": 0, "orders": 0, "visitors": 0, "signups": 0} for d in dates}
    for o in paid_cur:
        d = series.get(_day(o["created_at"]))
        if d:
            d["orders"] += 1
            if o["currency"] == "INR":
                d["revenue"] += o["amount"]

    status = Counter(o["status"] for o in cur)
    top: dict = defaultdict(lambda: {"name": "", "orders": 0, "units": 0, "revenue": 0})
    by_type: dict = defaultdict(lambda: {"orders": 0, "revenue": 0})
    sources: dict = defaultdict(lambda: {"orders": 0, "revenue": 0})
    codes: Counter = Counter()
    for o in paid_cur:
        key = o.get("product_id") or f"{o.get('product')}:{o.get('design_name')}"
        t = top[key]
        t["name"] = o.get("design_name") or "Untitled"
        t["kind"] = "shop" if o.get("product_id") else (o.get("product") or "custom")
        t["orders"] += 1
        t["units"] += o.get("qty", 1)
        bt = by_type[TYPE_LABEL.get("catalog" if o.get("product_id") else o.get("product"), "Other")]
        bt["orders"] += 1
        src = ((o.get("attribution") or {}).get("source")) or "direct"
        sources[src]["orders"] += 1
        if o["currency"] == "INR":
            t["revenue"] += o["amount"]
            bt["revenue"] += o["amount"]
            sources[src]["revenue"] += o["amount"]
        if o.get("discount_label"):
            codes[o["discount_label"]] += 1

    # ---- customers
    signups = await db.users.find({"created_at": {"$gte": prev_start.isoformat()}}, {"_id": 0, "created_at": 1}).to_list(MAX_DOCS)
    new_cur = [u for u in signups if u["created_at"] >= start.isoformat()]
    for u in new_cur:
        d = series.get(_day(u["created_at"]))
        if d:
            d["signups"] += 1
    buyers = {o["user_id"] for o in paid_cur}
    earlier_buyers = set()
    if buyers:
        earlier_buyers = {o["user_id"] async for o in db.orders.find(
            {"user_id": {"$in": list(buyers)}, "status": {"$in": list(PAID)}, "created_at": {"$lt": start.isoformat()}}, {"user_id": 1})}

    # ---- traffic (first-party events)
    events = await db.events.find({"at": {"$gte": prev_start}}, {"_id": 0, "event": 1, "anon_id": 1, "at": 1, "platform": 1}).to_list(MAX_DOCS)
    visitors_cur, visitors_prev = set(), set()
    per_day: dict = defaultdict(set)
    funnel: dict = defaultdict(set)
    platforms: dict = defaultdict(set)
    for e in events:
        at = e["at"] if e["at"].tzinfo else e["at"].replace(tzinfo=timezone.utc)
        if at >= start:
            visitors_cur.add(e["anon_id"])
            per_day[_day(at)].add(e["anon_id"])
            funnel[e["event"]].add(e["anon_id"])
            platforms[e.get("platform") or "web"].add(e["anon_id"])
        else:
            visitors_prev.add(e["anon_id"])
    for d, who in per_day.items():
        if d in series:
            series[d]["visitors"] = len(who)

    # ---- things that need attention now
    low_stock = 0  # live products with a size at 3 or fewer (the catalog is small, so count here)
    async for p in db.catalog_products.find({"status": "active"}, {"stock": 1}):
        if p.get("stock") and min(p["stock"].values()) <= 3:
            low_stock += 1
    attention = {
        "to_ship": await db.orders.count_documents({"status": {"$in": ["paid", "printed"]}}),
        "low_stock": low_stock,
        "pending_products": await db.catalog_products.count_documents({"status": "pending"}),
        "pending_creators": await db.creators.count_documents({"status": "pending"}),
        "refunds_due": await db.orders.count_documents({"status": "cancelled", "refund_due": True}),
    }

    n_cur, n_prev = len(paid_cur), len(paid_prev)
    rev_cur, rev_prev = inr(paid_cur), inr(paid_prev)
    inr_orders = [o for o in paid_cur if o["currency"] == "INR"]
    v_cur = len(visitors_cur)
    return {
        "days": days,
        "from": dates[0],
        "to": dates[-1],
        "kpis": {
            "revenue_inr": rev_cur, "revenue_inr_change": _pct(rev_cur, rev_prev),
            "revenue": revenue(paid_cur),
            "orders": n_cur, "orders_change": _pct(n_cur, n_prev),
            "aov_inr": round(rev_cur / len(inr_orders)) if inr_orders else 0,
            "new_customers": len(new_cur), "new_customers_change": _pct(len(new_cur), len(signups) - len(new_cur)),
            "visitors": v_cur, "visitors_change": _pct(v_cur, len(visitors_prev)),
            "conversion": round(len(buyers) / v_cur * 100, 2) if v_cur else None,
            "repeat_buyers": len(buyers & earlier_buyers),
            "cancelled": status.get("cancelled", 0),
        },
        "attention": attention,
        "series": [series[d] for d in dates],
        "status": {k: status.get(k, 0) for k in ("paid", "printed", "shipped", "delivered", "cancelled")},
        "top_products": sorted(top.values(), key=lambda t: (-t["revenue"], -t["units"]))[:8],
        "by_type": sorted(({"type": k, **v} for k, v in by_type.items()), key=lambda r: -r["orders"]),
        "sources": sorted(({"source": k, **v} for k, v in sources.items()), key=lambda r: -r["orders"])[:8],
        "codes": [{"code": k, "orders": v} for k, v in codes.most_common(6)],
        "funnel": {k: len(funnel.get(k, ())) for k in ("visit", "product_view", "design_started", "checkout_started")} | {"purchase": len(buyers)},
        "platforms": {k: len(v) for k, v in platforms.items()},
    }
