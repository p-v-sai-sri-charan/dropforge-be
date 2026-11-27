import asyncio
from datetime import datetime, timezone
from mongomock_motor import AsyncMongoMockClient
import core
mdb = AsyncMongoMockClient()["t_promos"]
import birthday, store_routes
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

pushed, mailed = [], []


@pytest.fixture(autouse=True, scope="module")
def _this_modules_db():
    core.db = mdb
    for m in (birthday, store_routes):
        m.db = mdb

    async def fake_push(uid, title, body, data=None):
        pushed.append((uid, body)); return 1

    async def fake_mail(to, subject, html, text):
        mailed.append(to); return True
    birthday.notify_user = fake_push
    birthday.send_email = fake_mail
    birthday.email_enabled = lambda: True


app = FastAPI(); app.include_router(store_routes.router); c = TestClient(app)
run = lambda co: asyncio.new_event_loop().run_until_complete(co)
H = lambda uid: {"Authorization": "Bearer " + core.make_token(uid)}
NOON_IST_JUN5 = datetime(2026, 6, 5, 7, 0, tzinfo=timezone.utc)


def test_birthday_offer_once_per_year():
    run(mdb.users.insert_many([
        {"id": "u1", "phone": "+91a", "name": "Ravi K", "dob": "1999-06-05", "email": "r@x.in", "email_verified": True},
        {"id": "u2", "phone": "+91b", "dob": "2001-06-05", "push_promotions": False, "email": "n@x.in"},
        {"id": "u3", "phone": "+91c", "dob": "2001-06-06"},
    ]))
    assert run(birthday.send_birthday_offers(datetime(2026, 6, 4, 20, 0, tzinfo=timezone.utc))) == 0  # 1:30am IST: too early
    assert run(birthday.send_birthday_offers(NOON_IST_JUN5)) == 2
    assert run(birthday.send_birthday_offers(NOON_IST_JUN5)) == 0  # already sent this year
    assert [p[0] for p in pushed] == ["u1"] and mailed == ["r@x.in"]  # u2 opted out of promos, email not verified
    d = run(mdb.discounts.find_one({"user_id": "u1"}))
    assert d["code"].startswith("BDAY") and d["percent"] == 15 and d["expires_at"] == "2026-06-12" and d["max_uses"] == 1


def test_personal_code_only_for_its_owner():
    code = run(mdb.discounts.find_one({"user_id": "u1"}))["code"]
    run(mdb.discounts.update_one({"code": code}, {"$set": {"expires_at": "2999-01-01"}}))  # the test birthday is in the past
    assert run(store_routes.find_discount(code, {"id": "u1"}, {}))["percent"] == 15
    with pytest.raises(HTTPException):
        run(store_routes.find_discount(code, {"id": "u3"}, {}))
    run(mdb.orders.insert_one({"id": "o1", "user_id": "u1", "discount_label": code, "status": "paid", "amount": 1000, "currency": "INR"}))
    with pytest.raises(HTTPException):
        run(store_routes.find_discount(code, {"id": "u1"}, {}))


def test_leap_day_birthdays():
    assert birthday._birthday_keys(datetime(2027, 2, 28).date()) == ["-02-28", "-02-29"]
    assert birthday._birthday_keys(datetime(2028, 2, 28).date()) == ["-02-28"]


def test_creator_codes():
    run(mdb.users.insert_one({"id": "cr", "phone": "+91d"}))
    run(mdb.creators.insert_one({"id": "c1", "user_id": "cr", "status": "approved", "slug": "ava", "display_name": "Ava"}))
    assert c.post("/api/creators/codes", headers=H("u3"), json={"code": "AVA10", "percent": 10}).status_code == 404  # not a creator
    assert c.post("/api/creators/codes", headers=H("cr"), json={"code": "AVA30", "percent": 30}).status_code == 400  # above the 20% cap
    r = c.post("/api/creators/codes", headers=H("cr"), json={"code": "ava10", "percent": 10, "max_uses": 2})
    assert r.status_code == 200 and r.json()["code"] == "AVA10"
    assert c.post("/api/creators/codes", headers=H("cr"), json={"code": "AVA10", "percent": 10}).status_code == 409
    mine, theirs = {"creator_id": "c1"}, {"creator_id": "c2"}
    assert run(store_routes.find_discount("ava10", {"id": "u3"}, mine))["percent"] == 10
    with pytest.raises(HTTPException):
        run(store_routes.find_discount("AVA10", {"id": "u3"}, theirs))  # someone else's product
    run(mdb.orders.insert_one({"id": "o2", "user_id": "u2", "discount_label": "AVA10", "status": "delivered", "amount": 90000, "currency": "INR"}))
    run(mdb.discounts.update_one({"code": "AVA10"}, {"$set": {"uses": 2}}))
    with pytest.raises(HTTPException):
        run(store_routes.find_discount("AVA10", {"id": "u3"}, mine))  # cap reached
    out = c.get("/api/creators/codes", headers=H("cr")).json()
    assert out["max_pct"] == 20 and out["codes"][0]["orders"] == 1 and out["codes"][0]["revenue"] == {"INR": 90000}
    assert c.patch("/api/creators/codes/AVA10", headers=H("cr"), json={"active": False}).json()["active"] is False
    assert c.patch("/api/creators/codes/BDAYX", headers=H("cr"), json={"active": False}).status_code == 404
