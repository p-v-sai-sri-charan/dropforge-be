import asyncio
from datetime import datetime, timedelta, timezone
from mongomock_motor import AsyncMongoMockClient
import core
mdb = AsyncMongoMockClient()["orders_extra"]
import orders_routes, reminders, push, email_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

pushed, emailed = [], []


@pytest.fixture(autouse=True, scope="module")
def _this_modules_db():
    core.db = mdb
    for m in (orders_routes, reminders, push, email_routes):
        m.db = mdb
    orders_routes.check_pin = lambda pin, ip: None
    orders_routes.send_sms = lambda to, text: False

    async def fake_push(uid, title, body, data=None):
        pushed.append((uid, title)); return 1

    async def fake_email(order, kind, extra=None):
        emailed.append((order["id"], kind)); return True
    for m in (orders_routes, reminders):
        m.notify_user = fake_push
        m.email_order = fake_email


app = FastAPI(); app.include_router(orders_routes.router); c = TestClient(app)
run = lambda co: asyncio.new_event_loop().run_until_complete(co)
ago = lambda **kw: (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def test_shipped_with_tracking():
    run(mdb.orders.insert_one({"id": "s1", "user_id": "u", "phone": "+91", "status": "printed", "design_name": "Tee", "kind": "catalog", "size": "M", "qty": 1}))
    r = c.patch("/api/admin/orders/s1/status", json={"status": "shipped", "courier": "Shiprocket", "tracking_number": "AWB123"}).json()
    assert r["tracking_url"] == "https://shiprocket.co/tracking/AWB123" and r["courier"] == "Shiprocket" and r["email_sent"] is True
    assert ("s1", "shipped") in emailed
    # correct it later with a direct link
    r = c.patch("/api/admin/orders/s1/tracking", json={"courier": "Delhivery", "tracking_number": "DL9", "tracking_url": "https://www.delhivery.com/track/package/DL9"}).json()
    assert r["tracking_url"].endswith("/DL9") and r["courier"] == "Delhivery"
    assert c.patch("/api/admin/orders/s1/tracking", json={"tracking_url": "javascript:alert(1)"}).status_code == 422
    assert c.patch("/api/admin/orders/s1/tracking", json={"tracking_number": "x y"}).status_code == 422


def test_tracking_only_for_shipped():
    run(mdb.orders.insert_one({"id": "s2", "user_id": "u", "status": "paid"}))
    assert c.patch("/api/admin/orders/s2/tracking", json={"tracking_number": "A1"}).status_code == 400


def test_abandoned_checkout_reminders():
    pushed.clear(); emailed.clear()
    run(mdb.orders.insert_many([
        {"id": "a1", "user_id": "r1", "status": "created", "design_name": "Hoodie", "created_at": ago(hours=3)},   # remind
        {"id": "a2", "user_id": "r2", "status": "created", "design_name": "Tee", "created_at": ago(minutes=20)},  # too soon
        {"id": "a3", "user_id": "r3", "status": "created", "design_name": "Tote", "created_at": ago(days=3)},     # too old
        {"id": "a4", "user_id": "r4", "status": "created", "design_name": "Crop", "created_at": ago(hours=5)},    # paid on a later try
        {"id": "a5", "user_id": "r4", "status": "paid", "design_name": "Crop", "created_at": ago(hours=4)},
    ]))
    assert run(reminders.remind_abandoned_checkouts()) == 1
    assert pushed == [("r1", "Your design is waiting")] and emailed == [("a1", "reminder")]
    assert run(reminders.remind_abandoned_checkouts()) == 0  # never twice
    assert run(mdb.orders.find_one({"id": "a4"})).get("reminded_at")  # claimed, but skipped
