# Iteration 2: auth (OTP), features, config (multi-product), files, designs, orders, hoodie model
import io
import os
import time
import uuid

import jwt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

FE = dotenv_values("/app/frontend/.env")
BE = dotenv_values("/app/backend/.env")
BASE_URL = (os.environ.get("EXPO_BACKEND_URL") or FE["EXPO_PUBLIC_BACKEND_URL"]).strip('"').rstrip("/")
PIN = "2468"
PHONE, CODE = "+15550001234", "246810"

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


@pytest.fixture(scope="session")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/verify-otp", json={"phone": PHONE, "code": CODE})
    if r.status_code != 200:
        pytest.skip(f"login failed {r.status_code} {r.text}")
    return r.json()["token"]


@pytest.fixture(scope="session")
def other_token():
    # second user minted directly (only test phone can log in without Twilio)
    m = MongoClient(BE["MONGO_URL"].strip('"'))[BE["DB_NAME"].strip('"')]
    uid = "TEST_other_" + uuid.uuid4().hex[:8]
    m.users.insert_one({"id": uid, "phone": "+15559990000", "created_at": "x"})
    yield jwt.encode({"sub": uid, "exp": int(time.time()) + 3600}, BE["JWT_SECRET"].strip('"'), algorithm="HS256")
    m.users.delete_one({"id": uid})


def H(t):
    return {"Authorization": f"Bearer {t}"}


# --- Auth ---
class TestAuth:
    def test_send_otp_test_phone(self):
        r = requests.post(f"{BASE_URL}/api/auth/send-otp", json={"phone": PHONE})
        assert r.status_code == 200 and r.json() == {"sent": True}

    def test_send_otp_real_phone_503(self):
        r = requests.post(f"{BASE_URL}/api/auth/send-otp", json={"phone": "+919876543210"})
        assert r.status_code == 503 and "SMS login isn't set up yet" in r.json()["detail"]

    def test_send_otp_bad_format(self):
        r = requests.post(f"{BASE_URL}/api/auth/send-otp", json={"phone": "12345678"})
        assert r.status_code == 400

    def test_verify_wrong_code(self):
        r = requests.post(f"{BASE_URL}/api/auth/verify-otp", json={"phone": PHONE, "code": "000000"})
        assert r.status_code == 400

    def test_verify_and_me(self, token):
        r = requests.get(f"{BASE_URL}/api/auth/me", headers=H(token))
        assert r.status_code == 200 and r.json()["phone"] == PHONE

    def test_me_no_token(self):
        assert requests.get(f"{BASE_URL}/api/auth/me").status_code == 401


# --- Features / config / model ---
class TestConfig:
    def test_features(self):
        assert requests.get(f"{BASE_URL}/api/features").json() == {"payments": False, "sms": False}

    def test_get_config_products(self):
        c = requests.get(f"{BASE_URL}/api/config").json()
        for p in ("tee", "hoodie", "tote"):
            assert set(c["products"][p]["prices"]) == {"INR", "USD", "GBP", "EUR"}
            assert "front" in c["products"][p] and "back" in c["products"][p]

    def test_put_config_validation(self):
        c = requests.get(f"{BASE_URL}/api/config").json()
        ok = requests.put(f"{BASE_URL}/api/config", json=c, headers={"X-Admin-Pin": PIN})
        assert ok.status_code == 200
        bad = {**c, "products": {k: v for k, v in c["products"].items() if k != "tote"}}
        assert requests.put(f"{BASE_URL}/api/config", json=bad, headers={"X-Admin-Pin": PIN}).status_code == 422
        prods = {k: dict(v) for k, v in c["products"].items()}
        prods["tee"] = {**prods["tee"], "prices": {"INR": 799, "USD": 19, "GBP": 16}}
        assert requests.put(f"{BASE_URL}/api/config", json={**c, "products": prods}, headers={"X-Admin-Pin": PIN}).status_code == 422

    def test_put_config_no_pin(self):
        c = requests.get(f"{BASE_URL}/api/config").json()
        assert requests.put(f"{BASE_URL}/api/config", json=c).status_code == 401

    def test_hoodie_glb(self):
        r = requests.get(f"{BASE_URL}/api/models/hoodie.glb")
        assert r.status_code == 200 and 900_000 < len(r.content) < 1_400_000


# --- Files + designs ---
class TestFilesDesigns:
    def test_upload_and_access(self, token, other_token):
        r = requests.post(f"{BASE_URL}/api/upload", files={"file": ("a.png", io.BytesIO(PNG), "image/png")}, headers=H(token))
        assert r.status_code == 200, r.text
        url = r.json()["url"]
        assert url.startswith("/api/files/")
        g = requests.get(f"{BASE_URL}{url}?token={token}")
        assert g.status_code == 200 and g.content == PNG
        assert requests.get(f"{BASE_URL}{url}?token={other_token}").status_code == 404
        assert requests.get(f"{BASE_URL}{url}").status_code == 401
        pytest.uploaded_url = url

    def test_upload_non_image(self, token):
        r = requests.post(f"{BASE_URL}/api/upload", files={"file": ("a.txt", b"hi", "text/plain")}, headers=H(token))
        assert r.status_code == 400

    def test_upload_no_auth(self):
        r = requests.post(f"{BASE_URL}/api/upload", files={"file": ("a.png", io.BytesIO(PNG), "image/png")})
        assert r.status_code == 401

    def test_design_crud_scoped(self, token, other_token):
        did = "TEST_" + uuid.uuid4().hex[:10]
        url = getattr(pytest, "uploaded_url", "/api/files/x")
        body = {"name": "TEST design", "product": "hoodie", "color": "#111111",
                "front": [{"id": "l1", "kind": "image", "src": url, "x": 0.5, "y": 0.5, "scale": 1, "rotation": 0}],
                "back": [{"id": "l2", "kind": "svg", "src": "<svg></svg>", "x": 0.5, "y": 0.5, "scale": 1, "rotation": 0}]}
        r = requests.put(f"{BASE_URL}/api/designs/{did}", json=body, headers=H(token))
        assert r.status_code == 200 and r.json()["product"] == "hoodie"
        g = requests.get(f"{BASE_URL}/api/designs/{did}", headers=H(token))
        assert g.status_code == 200 and g.json()["name"] == "TEST design" and "user_id" not in g.json()
        assert any(d["id"] == did for d in requests.get(f"{BASE_URL}/api/designs", headers=H(token)).json())
        assert requests.get(f"{BASE_URL}/api/designs/{did}", headers=H(other_token)).status_code == 404
        assert requests.put(f"{BASE_URL}/api/designs/{did}", json=body, headers=H(other_token)).status_code == 409
        # orders: 503 without keys
        ship = {"name": "Test", "phone": "+15550001234", "line1": "1 Road", "city": "Pune", "postal": "411001", "country": "India"}
        o = requests.post(f"{BASE_URL}/api/orders", json={"design_id": did, "size": "M", "qty": 1, "currency": "INR", "shipping": ship}, headers=H(token))
        assert o.status_code == 503 and "Card payments aren't set up yet" in o.json()["detail"]
        o2 = requests.post(f"{BASE_URL}/api/orders", json={"design_id": did, "size": "ONE SIZE", "qty": 1, "currency": "INR", "shipping": ship}, headers=H(token))
        assert o2.status_code == 400
        requests.delete(f"{BASE_URL}/api/designs/{did}", headers=H(other_token))
        assert requests.get(f"{BASE_URL}/api/designs/{did}", headers=H(token)).status_code == 200
        assert requests.delete(f"{BASE_URL}/api/designs/{did}", headers=H(token)).json() == {"ok": True}
        assert requests.get(f"{BASE_URL}/api/designs/{did}", headers=H(token)).status_code == 404

    def test_design_inline_image_rejected(self, token):
        body = {"name": "TEST bad", "product": "tee", "color": "#fff",
                "front": [{"id": "l1", "kind": "image", "src": "data:image/png;base64,AAA", "x": 0, "y": 0, "scale": 1, "rotation": 0}], "back": []}
        assert requests.put(f"{BASE_URL}/api/designs/TEST_bad", json=body, headers=H(token)).status_code == 422

    def test_designs_no_auth(self):
        assert requests.get(f"{BASE_URL}/api/designs").status_code == 401


# --- Orders ---
class TestOrders:
    def test_order_unknown_design(self, token):
        ship = {"name": "Test", "phone": "+15550001234", "line1": "1 Road", "city": "Pune", "postal": "411001", "country": "India"}
        r = requests.post(f"{BASE_URL}/api/orders", json={"design_id": "nope", "size": "M", "qty": 1, "currency": "USD", "shipping": ship}, headers=H(token))
        assert r.status_code == 404

    def test_verify_fake(self, token):
        r = requests.post(f"{BASE_URL}/api/orders/verify", json={"razorpay_order_id": "order_x", "razorpay_payment_id": "pay_x", "razorpay_signature": "bad"}, headers=H(token))
        assert r.status_code in (400, 404)

    def test_list_orders(self, token):
        r = requests.get(f"{BASE_URL}/api/orders", headers=H(token))
        assert r.status_code == 200 and isinstance(r.json(), list)

    def test_admin_orders_pin(self):
        assert requests.get(f"{BASE_URL}/api/admin/orders").status_code == 401
        r = requests.get(f"{BASE_URL}/api/admin/orders", headers={"X-Admin-Pin": PIN})
        assert r.status_code == 200 and isinstance(r.json(), list)
