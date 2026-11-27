import asyncio
from datetime import datetime, timedelta, timezone
from mongomock_motor import AsyncMongoMockClient
import core
mdb = AsyncMongoMockClient()["t_analytics"]
import analytics_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


@pytest.fixture(autouse=True, scope="module")
def _this_modules_db():
    core.db = mdb
    analytics_routes.db = mdb
    analytics_routes.check_pin = lambda *a: None


app = FastAPI(); app.include_router(analytics_routes.router); c = TestClient(app)
run = lambda co: asyncio.new_event_loop().run_until_complete(co)
now = datetime.now(timezone.utc)
iso = lambda d: (now - timedelta(days=d)).isoformat()


def test_dashboard_numbers():
    run(mdb.orders.insert_many([
        {"id": "a", "user_id": "u1", "status": "delivered", "amount": 100000, "currency": "INR", "created_at": iso(1), "product": "catalog", "product_id": "p1",
         "design_name": "Moon", "qty": 2, "attribution": {"source": "instagram"}, "discount_label": "AVA10"},
        {"id": "b", "user_id": "u2", "status": "paid", "amount": 50000, "currency": "INR", "created_at": iso(2), "product": "tee", "design_name": "Mine", "qty": 1},
        {"id": "c", "user_id": "u3", "status": "paid", "amount": 3000, "currency": "USD", "created_at": iso(3), "product": "hoodie", "design_name": "X", "qty": 1},
        {"id": "d", "user_id": "u2", "status": "cancelled", "amount": 9000, "currency": "INR", "created_at": iso(4), "product": "tee", "design_name": "Y", "qty": 1},
        {"id": "e", "user_id": "u9", "status": "created", "amount": 9000, "currency": "INR", "created_at": iso(1), "product": "tee", "design_name": "Z", "qty": 1},
        {"id": "f", "user_id": "u1", "status": "delivered", "amount": 75000, "currency": "INR", "created_at": iso(40), "product": "tee", "design_name": "Old", "qty": 1},
    ]))
    run(mdb.users.insert_many([{"id": "u1", "created_at": iso(45)}, {"id": "u2", "created_at": iso(2)}, {"id": "u3", "created_at": iso(3)}]))
    run(mdb.events.insert_many([{"event": "visit", "anon_id": f"v{i}", "at": now - timedelta(days=1), "platform": "web"} for i in range(10)]
                               + [{"event": "visit", "anon_id": "old", "at": now - timedelta(days=35)}]))
    run(mdb.catalog_products.insert_many([{"id": "p1", "status": "active", "stock": {"M": 2, "L": 9}}, {"id": "p2", "status": "pending", "stock": {"M": 9}}]))
    r = c.get("/api/admin/analytics?days=30").json()
    k = r["kpis"]
    assert k["revenue_inr"] == 150000 and k["revenue"] == {"INR": 150000, "USD": 3000} and k["orders"] == 3
    assert k["revenue_inr_change"] == 100.0 and k["aov_inr"] == 75000 and k["cancelled"] == 1
    assert k["new_customers"] == 2 and k["visitors"] == 10 and k["visitors_change"] == 900.0 and k["repeat_buyers"] == 1
    assert k["conversion"] == 30.0
    assert len(r["series"]) == 30 and sum(d["revenue"] for d in r["series"]) == 150000 and sum(d["orders"] for d in r["series"]) == 3
    assert r["status"]["paid"] == 2 and r["status"]["delivered"] == 1
    assert r["top_products"][0]["name"] == "Moon" and r["top_products"][0]["units"] == 2
    assert {x["source"] for x in r["sources"]} == {"instagram", "direct"} and r["codes"] == [{"code": "AVA10", "orders": 1}]
    assert r["attention"] == {"to_ship": 2, "low_stock": 1, "pending_products": 1, "pending_creators": 0, "refunds_due": 0}
    assert r["funnel"]["visit"] == 10 and r["funnel"]["purchase"] == 3
