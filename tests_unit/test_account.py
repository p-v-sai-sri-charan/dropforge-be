import os, sys, asyncio, hashlib
from mongomock_motor import AsyncMongoMockClient
import core
mdb = AsyncMongoMockClient()["t3"]; core.db = mdb
import files_routes, account_routes, reviews_routes
files_routes.db = mdb; account_routes.db = mdb; reviews_routes.db = mdb

import pytest


@pytest.fixture(autouse=True, scope="module")
def _this_modules_db():
    """Each test module has its own in-memory database; point the shared sign-in code (core.db) at it."""
    core.db = mdb
    for m in (files_routes, account_routes, reviews_routes):
        m.db = mdb

files_routes.cloudinary_ready = lambda: False
from fastapi import FastAPI
from fastapi.testclient import TestClient
app = FastAPI(); app.include_router(account_routes.router); c = TestClient(app)
run = lambda co: asyncio.new_event_loop().run_until_complete(co)
H = lambda uid: {"Authorization": "Bearer " + core.make_token(uid)}

def seed(uid, phone):
    run(mdb.users.insert_one({"id": uid, "phone": phone, "email": "a@b.co"}))
    run(mdb.designs.insert_one({"id": "d-" + uid, "user_id": uid, "front": [{"kind": "image", "src": f"/api/files/{uid}/art.png"}], "back": [], "thumb": f"/api/files/{uid}/thumb.png"}))
    run(mdb.files.insert_many([{"path": f"{uid}/art.png", "owner_id": uid, "is_deleted": False}, {"path": f"{uid}/thumb.png", "owner_id": uid, "is_deleted": False},
                               {"path": f"{uid}/printed.png", "owner_id": uid, "is_deleted": False}]))
    run(mdb.push_tokens.insert_one({"token": "ExponentPushToken[x]", "user_id": uid}))
    run(mdb.ai_credits.insert_one({"user_id": uid, "paid": 30}))
    run(mdb.events.insert_one({"event": "visit", "user_id": uid}))

def test_blocked_while_order_on_the_way_then_delete():
    seed("u1", "+919876543210")
    run(mdb.orders.insert_one({"id": "o1", "user_id": "u1", "status": "shipped", "design_snapshot": {"front": [{"src": "/api/files/u1/printed.png"}]}}))
    r = c.get("/api/account/deletion", headers=H("u1")).json()
    assert r["designs"] == 1 and r["ai_credits"] == 30 and r["blockers"] and "on the way" in r["blockers"][0]
    assert c.post("/api/account/delete", headers=H("u1"), json={"confirm": "DELETE"}).status_code == 409
    run(mdb.orders.update_one({"id": "o1"}, {"$set": {"status": "delivered"}}))
    assert c.post("/api/account/delete", headers=H("u1"), json={"confirm": "yes"}).status_code == 422  # must type DELETE
    assert c.post("/api/account/delete", headers=H("u1"), json={"confirm": "DELETE"}).json() == {"ok": True}
    assert run(mdb.users.find_one({"id": "u1"})) is None
    assert run(mdb.designs.count_documents({"user_id": "u1"})) == 0
    assert run(mdb.push_tokens.count_documents({"user_id": "u1"})) == 0 and run(mdb.ai_credits.count_documents({"user_id": "u1"})) == 0
    assert run(mdb.events.find_one({}))["user_id"] is None
    o = run(mdb.orders.find_one({"id": "o1"}))
    assert o["account_deleted"] is True  # order record kept
    files = {f["path"]: f["is_deleted"] for f in [run(mdb.files.find_one({"path": p})) for p in ("u1/art.png", "u1/thumb.png", "u1/printed.png")]}
    assert files == {"u1/art.png": True, "u1/thumb.png": True, "u1/printed.png": False}  # the printed artwork stays for the order
    d = run(mdb.deleted_accounts.find_one({}))
    assert d["phone_sha256"] == hashlib.sha256(b"+919876543210").hexdigest() and "phone" not in d
    assert c.get("/api/account/deletion", headers=H("u1")).status_code == 401  # token no longer maps to a user

def test_creator_owed_money_and_admin_blocked():
    seed("u2", "+919999999999")
    run(mdb.creators.insert_one({"id": "cr2", "user_id": "u2", "status": "approved", "pan": "X"}))
    run(mdb.catalog_products.insert_one({"id": "p2", "creator_id": "cr2", "status": "active"}))
    run(mdb.earnings.insert_one({"creator_id": "cr2", "status": "pending", "amount": 500}))
    assert "earnings" in c.post("/api/account/delete", headers=H("u2"), json={"confirm": "DELETE"}).json()["detail"]
    run(mdb.earnings.update_many({}, {"$set": {"status": "paid"}}))
    assert c.post("/api/account/delete", headers=H("u2"), json={"confirm": "DELETE"}).status_code == 200
    assert run(mdb.creators.count_documents({"user_id": "u2"})) == 0
    assert run(mdb.catalog_products.find_one({"id": "p2"}))["status"] == "hidden"
    seed("adm", "+910000000001")
    assert "Admin" in c.post("/api/account/delete", headers=H("adm"), json={"confirm": "DELETE"}).json()["detail"]
