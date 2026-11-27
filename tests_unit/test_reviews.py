import asyncio
from mongomock_motor import AsyncMongoMockClient
import core
mdb = AsyncMongoMockClient()["t_reviews"]; core.db = mdb
import reviews_routes, store_routes
import pytest


@pytest.fixture(autouse=True, scope="module")
def _this_modules_db():
    core.db = mdb
    for m in (reviews_routes, store_routes):
        m.db = mdb

from fastapi import FastAPI
from fastapi.testclient import TestClient
app = FastAPI(); app.include_router(reviews_routes.router); app.include_router(store_routes.router); c = TestClient(app)
run = lambda co: asyncio.new_event_loop().run_until_complete(co)
H = lambda uid: {"Authorization": "Bearer " + core.make_token(uid)}

run(mdb.users.insert_many([{"id": "b1", "phone": "+911", "name": "Asha Rao"}, {"id": "b2", "phone": "+912", "name": ""}]))
run(mdb.catalog_products.insert_many([
    {"id": "p1", "status": "active", "name": "Moon Hoodie", "description": "glow print", "tags": ["space"], "created_at": "2"},
    {"id": "p2", "status": "active", "name": "Sun Tee", "description": "warm 100% cotton", "created_at": "1"},
]))


def test_only_delivered_buyers_and_one_per_user():
    assert c.post("/api/products/p1/reviews", headers=H("b1"), json={"rating": 5}).status_code == 403
    run(mdb.orders.insert_one({"id": "o1", "user_id": "b1", "product_id": "p1", "status": "delivered"}))
    run(mdb.orders.insert_one({"id": "o2", "user_id": "b2", "product_id": "p1", "status": "delivered"}))
    assert c.post("/api/products/p1/reviews", headers=H("b1"), json={"rating": 6}).status_code == 422
    r = c.post("/api/products/p1/reviews", headers=H("b1"), json={"rating": 4, "text": "  soft   and warm "}).json()
    assert r["text"] == "soft and warm" and r["name"] == "Asha · Verified buyer" and "user_id" not in r
    c.post("/api/products/p1/reviews", headers=H("b1"), json={"rating": 2})  # edit, not a second review
    c.post("/api/products/p1/reviews", headers=H("b2"), json={"rating": 5})
    out = c.get("/api/products/p1/reviews").json()
    assert out["count"] == 2 and out["average"] == 3.5
    assert c.get("/api/reviews/mine", headers=H("b1")).json() == {"p1": 2}


def test_admin_hide_updates_rating(monkeypatch):
    monkeypatch.setattr(reviews_routes, "check_pin", lambda *a: None)
    rid = run(mdb.reviews.find_one({"user_id": "b1"}))["id"]
    assert c.patch(f"/api/admin/reviews/{rid}", json={"hidden": True}).status_code == 200
    out = c.get("/api/products/p1/reviews").json()
    assert out["count"] == 1 and out["average"] == 5 and len(out["reviews"]) == 1


def test_shop_search():
    ids = lambda q: [p["id"] for p in c.get("/api/catalog/products", params={"q": q}).json()]
    assert ids("moon") == ["p1"] and ids("SPACE") == ["p1"] and ids("100%") == ["p2"]
    assert ids("sun cotton") == ["p2"] and ids("sun moon") == [] and ids(".*") == []
    assert len(ids("")) == 2
