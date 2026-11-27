import os, sys, asyncio, uuid
from datetime import datetime, timezone
import pytest
from mongomock_motor import AsyncMongoMockClient
import core
mdb = AsyncMongoMockClient()["t"]
core.db = mdb
import tracking_routes, push_routes, push, orders_routes
for m in (tracking_routes, push_routes, push, orders_routes): m.db = mdb

import pytest


@pytest.fixture(autouse=True, scope="module")
def _this_modules_db():
    """Each test module has its own in-memory database; point the shared sign-in code (core.db) at it."""
    core.db = mdb
    for m in (tracking_routes, push_routes, push, orders_routes,):
        m.db = mdb

from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()
app.include_router(tracking_routes.router); app.include_router(push_routes.router); app.include_router(orders_routes.router)
# admin: bypass the PIN/phone check (tested elsewhere) - patch where imported
for m in (tracking_routes, push_routes, orders_routes): m.check_pin = lambda pin, ip: None
c = TestClient(app)
run = lambda co: asyncio.get_event_loop().run_until_complete(co)

UID = "u1"; run(mdb.users.insert_one({"id": UID, "phone": "+911234567890"}))
H = {"Authorization": "Bearer " + core.make_token(UID)}
TOK = "ExponentPushToken[abcdefghijklmnop]"

import push as _p
REAL_SEND = _p.send_push
sent = []
async def fake_send(tokens, title, body, data=None):
    sent.append((list(tokens), title, body, data)); return {"sent": len(list(tokens)), "failed": 0}
push_routes.send_push = fake_send
push.send_push = fake_send

def test_track_and_attribution():
    a = {"source": " Instagram ", "medium": "social", "campaign": "Diwali"}
    assert c.post("/api/track", json={"event": "visit", "anon_id": "anon-1234", "platform": "web", "attribution": a}).status_code == 204
    assert c.post("/api/track", json={"event": "visit", "anon_id": "anon-1234", "platform": "web", "attribution": a}).status_code == 204
    assert c.post("/api/track", json={"event": "visit", "anon_id": "anon-5678", "platform": "android", "attribution": a}, headers=H).status_code == 204
    assert c.post("/api/track", json={"event": "product_view", "anon_id": "anon-5678", "platform": "android", "product_id": "p1", "attribution": a}).status_code == 204
    assert c.post("/api/track", json={"event": "visit", "anon_id": "anon-9999", "platform": "web"}).status_code == 204
    # signed-in visitor gets first-touch attribution on their profile, normalized
    u = run(mdb.users.find_one({"id": UID}))
    assert u["attribution"]["source"] == "instagram" and u["attribution"]["campaign"] == "diwali"
    # clients can't fake purchases or send junk
    assert c.post("/api/track", json={"event": "purchase", "anon_id": "anon-1234", "platform": "web"}).status_code == 422
    assert c.post("/api/track", json={"event": "visit", "anon_id": "bad id!", "platform": "web"}).status_code == 422

def test_campaign_report():
    now = datetime.now(timezone.utc).isoformat()
    run(mdb.orders.insert_many([
        {"id": "o1", "user_id": UID, "status": "paid", "paid_at": now, "currency": "INR", "amount": 129900, "attribution": {"source": "instagram", "campaign": "diwali"}},
        {"id": "o2", "user_id": UID, "status": "shipped", "paid_at": now, "currency": "USD", "amount": 2400, "attribution": {"source": "instagram", "campaign": "diwali"}},
        {"id": "o3", "user_id": UID, "status": "created", "currency": "INR", "amount": 5000},  # unpaid: not counted
        {"id": "o4", "user_id": UID, "status": "delivered", "paid_at": now, "currency": "INR", "amount": 99900},  # direct
    ]))
    r = c.get("/api/admin/marketing/campaigns?days=30").json()
    rows = {(x["source"], x["campaign"]): x for x in r["rows"]}
    ig = rows[("instagram", "diwali")]
    assert ig["visitors"] == 2 and ig["product_views"] == 1 and ig["orders"] == 2
    assert ig["revenue"] == {"INR": 129900, "USD": 2400}
    d = rows[("(direct)", "")]
    assert d["visitors"] == 1 and d["orders"] == 1 and d["revenue"] == {"INR": 99900}
    assert r["rows"][0]["source"] == "instagram"  # most orders first

def test_push_register_prefs_broadcast():
    assert c.post("/api/push/register", json={"token": TOK, "platform": "android"}).status_code == 401
    assert c.post("/api/push/register", json={"token": "not-a-token", "platform": "android"}, headers=H).status_code == 422
    assert c.post("/api/push/register", json={"token": TOK, "platform": "android"}, headers=H).json()["promotions"] is True
    assert c.post("/api/push/register", json={"token": TOK, "platform": "android"}, headers=H).status_code == 200  # idempotent
    assert run(mdb.push_tokens.count_documents({})) == 1
    assert c.post("/api/admin/push/broadcast", json={"title": "Sale", "body": "20% off hoodies"}).json() == {"devices": 1}  # dry run default
    assert sent == []
    r = c.post("/api/admin/push/broadcast", json={"title": "Sale", "body": "20% off hoodies", "url": "/shop", "dry_run": False}).json()
    assert r["status"] == "sending" and r["devices"] == 1  # returns at once; sending carries on in the background
    for _ in range(50):  # wait for the background send to finish
        done = c.get("/api/admin/push/campaigns").json()[0]
        if done.get("status") == "sent":
            break
        import time; time.sleep(0.05)
    assert done["status"] == "sent" and done["sent"] == 1 and sent[-1][3] == {"url": "/shop", "kind": "promotion"}
    assert c.post("/api/admin/push/broadcast", json={"title": "x", "body": "bad link", "url": "https://evil.example"}).status_code == 422
    assert c.post("/api/admin/push/broadcast", json={"title": "x", "body": "bad link", "url": "//evil.example"}).status_code == 422
    # opting out removes the user from promotions
    assert c.put("/api/push/prefs", json={"promotions": False}, headers=H).json() == {"promotions": False}
    assert c.post("/api/admin/push/broadcast", json={"title": "Sale", "body": "again"}).json() == {"devices": 0}
    assert len(c.get("/api/admin/push/campaigns").json()) == 1
    # sign-out removes the device
    assert c.post("/api/push/unregister", json={"token": TOK}).status_code == 200
    assert run(mdb.push_tokens.count_documents({})) == 0

def test_order_status_pushes_to_customer():
    run(mdb.push_tokens.insert_one({"token": TOK, "user_id": UID, "platform": "ios"}))
    orders_routes.send_sms = lambda to, text: False
    run(mdb.orders.insert_one({"id": "o9", "user_id": UID, "phone": "+911", "status": "paid", "design_name": "Neon Tee", "kind": "catalog", "size": "M", "qty": 1}))
    r = c.patch("/api/admin/orders/o9/status", json={"status": "shipped"}).json()
    assert r["status"] == "shipped" and r["push_sent"] == 1
    assert sent[-1][1] == "Your order has shipped" and sent[-1][0] == [TOK] and sent[-1][3] == {"url": "/orders"}

def test_dead_tokens_removed():
    import httpx
    run(mdb.push_tokens.insert_many([{"token": "ExponentPushToken[deaddeaddead1]", "user_id": "u2"}, {"token": "ExponentPushToken[aliveealive1]", "user_id": "u2"}]))
    class R:
        def raise_for_status(self): pass
        def json(self): return {"data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}}, {"status": "ok", "id": "x"}]}
    class Http:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json, headers): assert url.startswith("https://exp.host/") and len(json) == 2; return R()
    real_client = httpx.AsyncClient; httpx.AsyncClient = Http
    try:
        res = run(REAL_SEND(["ExponentPushToken[deaddeaddead1]", "ExponentPushToken[aliveealive1]"], "t", "b"))
    finally:
        httpx.AsyncClient = real_client
    assert res == {"sent": 1, "failed": 1}
    assert run(mdb.push_tokens.count_documents({"user_id": "u2"})) == 1

