"""Admin dashboard numbers: sales, orders, customers and traffic for a period, compared with the period before it,
plus daily series for charts. Days are counted in India time. Revenue = paid orders (paid, printed, shipped,
delivered), in minor units per currency; charts use INR.

All counting happens inside the database (aggregation pipelines on indexed fields); only small summaries come back,
so the cost doesn't depend on the server's memory and nothing is silently cut off."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Header, Query, Request

from core import IST, check_pin, client_ip, db

router = APIRouter(prefix="/api")

PAID = ["paid", "printed", "shipped", "delivered"]
TYPE_LABEL = {"catalog": "Shop products", "tee": "Custom tees", "hoodie": "Custom hoodies", "sweatshirt": "Custom sweatshirts", "tote": "Custom totes"}


def _pct(cur: float, prev: float) -> Optional[float]:
    return None if not prev else round((cur - prev) / prev * 100, 1)


async def _agg(coll: str, pipeline: list) -> list:
    return await db[coll].aggregate(pipeline, allowDiskUse=True).to_list(None)


async def _count_distinct(coll: str, match: dict, field: str) -> int:
    r = await _agg(coll, [{"$match": match}, {"$group": {"_id": f"${field}"}}, {"$group": {"_id": None, "n": {"$sum": 1}}}])
    return r[0]["n"] if r else 0


@router.get("/admin/analytics")
async def analytics(request: Request, days: int = Query(30, ge=7, le=365), x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    now = datetime.now(timezone.utc)
    today = now.astimezone(IST).date()
    first = today - timedelta(days=days - 1)
    prev_first = first - timedelta(days=days)
    dates = [(first + timedelta(days=i)).isoformat() for i in range(days)]
    d0, p0 = dates[0], prev_first.isoformat()
    start = datetime.combine(first, datetime.min.time(), IST).astimezone(timezone.utc)
    prev_start = start - timedelta(days=days)
    paid_now = {"status": {"$in": PAID}, "day": {"$gte": d0}}

    # ---- orders by day + status + currency (both periods): revenue, counts, status mix, daily series
    rows = await _agg("orders", [
        {"$match": {"day": {"$gte": p0}, "status": {"$ne": "created"}}},
        {"$group": {"_id": {"d": "$day", "s": "$status", "c": "$currency"}, "n": {"$sum": 1}, "amt": {"$sum": "$amount"}}},
    ])
    series = {d: {"date": d, "revenue": 0, "orders": 0, "visitors": 0, "signups": 0} for d in dates}
    revenue: dict = {}
    status = {k: 0 for k in ("paid", "printed", "shipped", "delivered", "cancelled")}
    n_cur = n_prev = rev_cur = rev_prev = inr_orders = 0
    for r in rows:
        d, s, c, n, amt = r["_id"]["d"], r["_id"]["s"], r["_id"]["c"], r["n"], r["amt"]
        cur = d >= d0
        if cur and s in status:
            status[s] += n
        if s not in PAID:
            continue
        if cur:
            n_cur += n
            revenue[c] = revenue.get(c, 0) + amt
            if d in series:
                series[d]["orders"] += n
            if c == "INR":
                rev_cur += amt
                inr_orders += n
                if d in series:
                    series[d]["revenue"] += amt
        else:
            n_prev += n
            if c == "INR":
                rev_prev += amt

    inr = {"$cond": [{"$eq": ["$currency", "INR"]}, "$amount", 0]}
    top = await _agg("orders", [
        {"$match": paid_now},
        {"$group": {"_id": {"pid": "$product_id", "p": "$product", "n": "$design_name"}, "orders": {"$sum": 1}, "units": {"$sum": "$qty"}, "revenue": {"$sum": inr}}},
        {"$sort": {"revenue": -1, "units": -1}},
        {"$limit": 8},
    ])
    by_type = await _agg("orders", [{"$match": paid_now}, {"$group": {"_id": "$product", "orders": {"$sum": 1}, "revenue": {"$sum": inr}}}, {"$sort": {"orders": -1}}])
    sources = await _agg("orders", [
        {"$match": paid_now},
        {"$group": {"_id": "$attribution.source", "orders": {"$sum": 1}, "revenue": {"$sum": inr}}},
        {"$sort": {"orders": -1}},
        {"$limit": 8},
    ])
    codes = await _agg("orders", [
        {"$match": {**paid_now, "discount_label": {"$type": "string"}}},
        {"$group": {"_id": "$discount_label", "orders": {"$sum": 1}}},
        {"$sort": {"orders": -1}},
        {"$limit": 6},
    ])
    buyers = await _count_distinct("orders", paid_now, "user_id")
    # repeat buyers: bought in this period and also before it
    repeat = await _agg("orders", [
        {"$match": {"status": {"$in": PAID}, "day": {"$type": "string"}}},
        {"$group": {"_id": "$user_id", "first": {"$min": "$day"}, "last": {"$max": "$day"}}},
        {"$match": {"first": {"$lt": d0}, "last": {"$gte": d0}}},
        {"$group": {"_id": None, "n": {"$sum": 1}}},
    ])

    # ---- sign-ups per day
    signups_cur = signups_prev = 0
    for r in await _agg("users", [{"$match": {"day": {"$gte": p0}}}, {"$group": {"_id": "$day", "n": {"$sum": 1}}}]):
        if r["_id"] >= d0:
            signups_cur += r["n"]
            if r["_id"] in series:
                series[r["_id"]]["signups"] = r["n"]
        else:
            signups_prev += r["n"]

    # ---- traffic (first-party events): unique visitors per day and per period, funnel, platforms
    for r in await _agg("events", [
        {"$match": {"at": {"$gte": start}}},
        {"$group": {"_id": {"d": "$day", "a": "$anon_id"}}},
        {"$group": {"_id": "$_id.d", "n": {"$sum": 1}}},
    ]):
        if r["_id"] in series:
            series[r["_id"]]["visitors"] = r["n"]
    visitors_cur = await _count_distinct("events", {"at": {"$gte": start}}, "anon_id")
    visitors_prev = await _count_distinct("events", {"at": {"$gte": prev_start, "$lt": start}}, "anon_id")
    funnel = {r["_id"]: r["n"] for r in await _agg("events", [
        {"$match": {"at": {"$gte": start}}},
        {"$group": {"_id": {"e": "$event", "a": "$anon_id"}}},
        {"$group": {"_id": "$_id.e", "n": {"$sum": 1}}},
    ])}
    platforms = {(r["_id"] or "web"): r["n"] for r in await _agg("events", [
        {"$match": {"at": {"$gte": start}}},
        {"$group": {"_id": {"p": "$platform", "a": "$anon_id"}}},
        {"$group": {"_id": "$_id.p", "n": {"$sum": 1}}},
    ])}

    # ---- things that need attention now (indexed counts)
    low_stock = 0  # live products with a size at 3 or fewer (catalog is small; stock is a per-size map)
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

    return {
        "days": days,
        "from": dates[0],
        "to": dates[-1],
        "kpis": {
            "revenue_inr": rev_cur, "revenue_inr_change": _pct(rev_cur, rev_prev),
            "revenue": revenue,
            "orders": n_cur, "orders_change": _pct(n_cur, n_prev),
            "aov_inr": round(rev_cur / inr_orders) if inr_orders else 0,
            "new_customers": signups_cur, "new_customers_change": _pct(signups_cur, signups_prev),
            "visitors": visitors_cur, "visitors_change": _pct(visitors_cur, visitors_prev),
            "conversion": round(buyers / visitors_cur * 100, 2) if visitors_cur else None,
            "repeat_buyers": repeat[0]["n"] if repeat else 0,
            "cancelled": status["cancelled"],
        },
        "attention": attention,
        "series": [series[d] for d in dates],
        "status": status,
        "top_products": [{"name": t["_id"].get("n") or "Untitled", "kind": "shop" if t["_id"].get("pid") else (t["_id"].get("p") or "custom"),
                          "orders": t["orders"], "units": t["units"], "revenue": t["revenue"]} for t in top],
        "by_type": [{"type": TYPE_LABEL.get(r["_id"], "Other"), "orders": r["orders"], "revenue": r["revenue"]} for r in by_type],
        "sources": [{"source": r["_id"] or "direct", "orders": r["orders"], "revenue": r["revenue"]} for r in sources],
        "codes": [{"code": r["_id"], "orders": r["orders"]} for r in codes],
        "funnel": {k: funnel.get(k, 0) for k in ("visit", "product_view", "design_started", "checkout_started")} | {"purchase": buyers},
        "platforms": platforms,
    }
