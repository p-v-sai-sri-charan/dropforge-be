# Iteration 4: catalog, discounts, store settings, quote, creators, shares, order status
import os
import uuid
from datetime import datetime, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

BASE_URL = dotenv_values("/app/frontend/.env")["EXPO_PUBLIC_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
PIN = {"X-Admin-Pin": "2468"}
_benv = dotenv_values("/app/backend/.env")
mdb = MongoClient(_benv["MONGO_URL"])[_benv["DB_NAME"]]
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


@pytest.fixture(scope="module")
def token():
    requests.post(f"{API}/auth/send-otp", json={"phone": "+15550001234"})
    r = requests.post(f"{API}/auth/verify-otp", json={"phone": "+15550001234", "code": "246810"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def auth(token):
    return {"Authorization": f"Bearer {token['token']}"}


@pytest.fixture(scope="module")
def cats():
    p = requests.post(f"{API}/admin/categories", json={"name": "TEST_Parent"}, headers=PIN).json()
    c = requests.post(f"{API}/admin/categories", json={"name": "TEST_Child", "parent_id": p["id"]}, headers=PIN).json()
    yield p, c
    for x in (c, p):
        requests.delete(f"{API}/admin/categories/{x['id']}", headers=PIN)


@pytest.fixture(scope="module")
def product(cats):
    body = {"category_id": cats[1]["id"], "name": "TEST_Tee", "prices": {"INR": 500, "USD": 10}, "stock": {"S": 3, "M": 0}, "status": "active"}
    r = requests.post(f"{API}/admin/products", json=body, headers=PIN)
    assert r.status_code == 200, r.text
    p = r.json()
    yield p
    requests.delete(f"{API}/admin/products/{p['id']}", headers=PIN)


# --- categories
def test_category_nesting_and_delete_guard(cats, product):
    p, c = cats
    assert c["parent_id"] == p["id"]
    allc = requests.get(f"{API}/catalog/categories").json()
    assert any(x["id"] == c["id"] for x in allc)
    assert requests.delete(f"{API}/admin/categories/{p['id']}", headers=PIN).status_code == 400
    assert requests.delete(f"{API}/admin/categories/{c['id']}", headers=PIN).status_code == 400
    r = requests.put(f"{API}/admin/categories/{c['id']}", json={"name": "TEST_Child2"}, headers=PIN)
    assert r.json()["name"] == "TEST_Child2"
    assert requests.post(f"{API}/admin/categories", json={"name": "x", "parent_id": "nope"}, headers=PIN).status_code == 404
    assert requests.post(f"{API}/admin/categories", json={"name": "x"}).status_code in (401, 403)


# --- products
def test_product_validation(cats):
    base = {"category_id": cats[1]["id"], "name": "TEST_Bad"}
    assert requests.post(f"{API}/admin/products", json={**base, "prices": {"USD": 5}, "stock": {"S": 1}}, headers=PIN).status_code == 422
    assert requests.post(f"{API}/admin/products", json={**base, "prices": {"INR": 5}, "stock": {}}, headers=PIN).status_code == 422
    many = {f"Z{i}": 1 for i in range(13)}
    assert requests.post(f"{API}/admin/products", json={**base, "prices": {"INR": 5}, "stock": many}, headers=PIN).status_code == 422


def test_product_crud_and_public(cats, product):
    pid = product["id"]
    assert requests.get(f"{API}/catalog/products/{pid}").json()["name"] == "TEST_Tee"
    lst = requests.get(f"{API}/catalog/products", params={"category_id": cats[1]["id"]}).json()
    assert [x["id"] for x in lst] == [pid]
    body = {**{k: product[k] for k in ("category_id", "name", "prices", "stock")}, "status": "hidden"}
    assert requests.put(f"{API}/admin/products/{pid}", json=body, headers=PIN).json()["status"] == "hidden"
    assert requests.get(f"{API}/catalog/products/{pid}").status_code == 404
    assert requests.get(f"{API}/catalog/products", params={"category_id": cats[1]["id"]}).json() == []
    assert any(x["id"] == pid for x in requests.get(f"{API}/admin/products", headers=PIN).json())
    body["status"] = "active"
    requests.put(f"{API}/admin/products/{pid}", json=body, headers=PIN)


# --- discounts + settings
def test_discounts_crud():
    code = f"TEST{uuid.uuid4().hex[:6]}".upper()
    r = requests.post(f"{API}/admin/discounts", json={"code": code.lower(), "percent": 15}, headers=PIN)
    assert r.status_code == 200 and r.json()["code"] == code
    assert requests.post(f"{API}/admin/discounts", json={"code": code, "percent": 15}, headers=PIN).status_code == 409
    r = requests.put(f"{API}/admin/discounts/{code}", json={"code": code, "percent": 25, "active": False}, headers=PIN)
    assert r.json()["percent"] == 25 and r.json()["active"] is False
    assert any(d["code"] == code for d in requests.get(f"{API}/admin/discounts", headers=PIN).json())
    requests.delete(f"{API}/admin/discounts/{code}", headers=PIN)
    assert not any(d["code"] == code for d in requests.get(f"{API}/admin/discounts", headers=PIN).json())


def test_store_settings_roundtrip():
    orig = requests.get(f"{API}/admin/store-settings", headers=PIN).json()
    r = requests.put(f"{API}/admin/store-settings", json={"first_order_pct": 12, "commission_pct": 18}, headers=PIN)
    assert r.status_code == 200
    assert requests.get(f"{API}/store/settings").json()["first_order_pct"] == 12
    assert requests.put(f"{API}/admin/store-settings", json={"first_order_pct": 95, "commission_pct": 1}, headers=PIN).status_code == 422
    requests.put(f"{API}/admin/store-settings", json=orig, headers=PIN)


# --- quote
def test_quote_rules(auth, product):
    pid = product["id"]
    codes = {"TESTQ50": {"percent": 50}, "TESTQOFF": {"percent": 50, "active": False}, "TESTQEXP": {"percent": 50, "expires_at": "2020-01-01"}}
    for c, extra in codes.items():
        requests.post(f"{API}/admin/discounts", json={"code": c, **extra}, headers=PIN)
    try:
        q = lambda **kw: requests.post(f"{API}/orders/quote", json={"product_id": pid, "size": "S", "qty": 1, "currency": "INR", **kw}, headers=auth)
        base = q().json()
        assert base["subtotal"] == 50000
        print("base quote", base)
        best = q(discount_code="testq50").json()
        assert best["discount_label"] == "TESTQ50" and best["total"] == 25000
        for bad in ("TESTQOFF", "TESTQEXP", "NOPE123"):
            assert q(discount_code=bad).status_code == 400, bad
        assert q(size="M").status_code == 400
        assert q(size="XL").status_code == 400
        assert q(currency="GBP").status_code == 400
        assert q(qty=4).status_code == 400
        assert requests.post(f"{API}/orders/quote", json={"product_id": pid, "size": "S", "qty": 1, "currency": "INR"}).status_code == 401
    finally:
        for c in codes:
            requests.delete(f"{API}/admin/discounts/{c}", headers=PIN)


# --- creators
@pytest.fixture(scope="module")
def creator_backup(token):
    orig = mdb.creators.find_one({"user_id": token["user"]["id"]})
    yield orig
    if orig:
        mdb.creators.replace_one({"_id": orig["_id"]}, orig, upsert=True)
    else:
        mdb.creators.delete_one({"user_id": token["user"]["id"]})


KYC = {"display_name": "TEST_Creator", "legal_name": "Test Person", "pan": "ABCDE1234F", "bank_account": "123456789012", "ifsc": "HDFC0001234",
       "address": "12 Test Street", "city": "Mumbai", "state": "MH", "pincode": "400001"}


def test_creator_flow(auth, cats, creator_backup):
    for k, v in (("pan", "abc1234"), ("ifsc", "HDFC1001234"), ("pincode", "4000"), ("bank_account", "12ab")):
        assert requests.post(f"{API}/creators/apply", json={**KYC, k: v}, headers=auth).status_code == 422, k
    r = requests.post(f"{API}/creators/apply", json=KYC, headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    me = requests.get(f"{API}/creators/me", headers=auth).json()
    assert me["creator"]["pan"] == "ABXXXXX34F" and me["creator"]["bank_account"] == "XXXX9012"
    body = {"category_id": cats[1]["id"], "name": "TEST_CreatorTee", "prices": {"INR": 400}, "stock": {"L": 2}, "status": "active"}
    assert requests.post(f"{API}/creators/products", json=body, headers=auth).status_code == 403
    files = {"file": ("a.png", PNG, "image/png")}
    assert requests.post(f"{API}/upload?public=true", files=files, headers=auth).status_code == 403
    cid = me["creator"]["id"]
    assert any(c["id"] == cid for c in requests.get(f"{API}/admin/creators", headers=PIN).json())
    assert requests.patch(f"{API}/admin/creators/{cid}", json={"status": "approved"}, headers=PIN).json()["status"] == "approved"
    r = requests.post(f"{API}/creators/products", json=body, headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    pid = r.json()["id"]
    try:
        assert requests.get(f"{API}/catalog/products/{pid}").status_code == 404
        adm = [p for p in requests.get(f"{API}/admin/products", headers=PIN).json() if p["id"] == pid]
        assert adm and adm[0]["creator_name"] == "TEST_Creator"
        up = requests.post(f"{API}/upload?public=true", files={"file": ("a.png", PNG, "image/png")}, headers=auth)
        print("creator public upload", up.status_code, up.text[:200])
        if up.status_code == 200:
            fr = requests.get(f"{BASE_URL}{up.json()['url']}")
            assert fr.status_code == 200, "public file not readable without token"
    finally:
        requests.delete(f"{API}/creators/products/{pid}", headers=auth)


def test_admin_upload_and_earnings():
    assert requests.post(f"{API}/admin/upload", files={"file": ("a.png", PNG, "image/png")}).status_code in (401, 403)
    r = requests.post(f"{API}/admin/upload", files={"file": ("a.png", PNG, "image/png")}, headers=PIN)
    print("admin upload", r.status_code, r.text[:200])
    if r.status_code == 200:
        assert requests.get(f"{BASE_URL}{r.json()['url']}").status_code == 200
    eid = f"TEST_{uuid.uuid4()}"
    mdb.earnings.insert_one({"id": eid, "creator_id": "TEST_x", "order_id": "x", "product_name": "x", "currency": "INR", "pct": 10, "amount": 100, "status": "pending", "created_at": "2026-01-01"})
    try:
        assert requests.patch(f"{API}/admin/earnings/{eid}", headers=PIN).json()["status"] == "paid"
    finally:
        mdb.earnings.delete_one({"id": eid})


# --- order status
def test_order_status(token, auth):
    now = datetime.now(timezone.utc).isoformat()
    base = {"user_id": token["user"]["id"], "phone": "+15550001234", "kind": "catalog", "design_name": "TEST_Order", "product": "catalog", "size": "S", "qty": 1,
            "currency": "INR", "amount": 50000, "history": [], "created_at": now}
    unpaid, paid = f"TEST_{uuid.uuid4()}", f"TEST_{uuid.uuid4()}"
    mdb.orders.insert_many([{**base, "id": unpaid, "status": "created"}, {**base, "id": paid, "status": "paid"}])
    try:
        assert requests.patch(f"{API}/admin/orders/{unpaid}/status", json={"status": "shipped"}, headers=PIN).status_code == 400
        r = requests.patch(f"{API}/admin/orders/{paid}/status", json={"status": "shipped"}, headers=PIN)
        assert r.status_code == 200 and r.json()["status"] == "shipped"
        print("sms_sent", r.json().get("sms_sent"))
        mine = {o["id"]: o for o in requests.get(f"{API}/orders", headers=auth).json()}
        assert mine[paid]["status"] == "shipped"
        assert requests.patch(f"{API}/admin/orders/{paid}/status", json={"status": "bogus"}, headers=PIN).status_code == 422
    finally:
        mdb.orders.delete_many({"id": {"$in": [unpaid, paid]}})


# --- shares
def test_shares(auth):
    did = f"TEST_{uuid.uuid4().hex[:8]}"
    requests.put(f"{API}/designs/{did}", json={"name": "TEST Share", "product": "tee", "color": "#ffffff", "front": [], "back": []}, headers=auth)
    try:
        r = requests.post(f"{API}/shares", json={"design_id": did}, headers=auth)
        assert r.status_code == 200, r.text
        slug, url = r.json()["slug"], r.json()["url"]
        assert url.startswith(BASE_URL), url
        h = requests.get(url)
        assert h.status_code == 200 and "text/html" in h.headers["content-type"]
        assert requests.get(f"{API}/shares/{slug}").json()["design"]["name"] == "TEST Share"
        assert requests.post(f"{API}/shares", json={"design_id": "nope"}, headers=auth).status_code == 404
        q = requests.post(f"{API}/orders/quote", json={"share_slug": slug, "size": "M", "qty": 1, "currency": "INR"}, headers=auth)
        assert q.status_code == 200, q.text
        mdb.shares.delete_one({"slug": slug})
    finally:
        requests.delete(f"{API}/designs/{did}", headers=auth)
    assert requests.get(f"{API}/s/t1-sy1jm6o").status_code == 200
    assert requests.get(f"{API}/s/does-not-exist").status_code == 404
