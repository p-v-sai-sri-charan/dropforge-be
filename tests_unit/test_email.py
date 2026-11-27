import asyncio, os, re
from mongomock_motor import AsyncMongoMockClient
import core
mdb = AsyncMongoMockClient()["email"]
import auth_routes, email_routes, mailer
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

sent = []


@pytest.fixture(autouse=True, scope="module")
def _this_modules_db():
    core.db = mdb
    for m in (auth_routes, email_routes):
        m.db = mdb
    os.environ.update(EMAIL_FROM="DropForge <orders@dropforge.in>", RESEND_API_KEY="re_test")

    async def fake_send(to, subject, html_body, text_body):
        sent.append({"to": to, "subject": subject, "html": html_body, "text": text_body})
        return True
    email_routes.send_email = fake_send
    yield
    for k in ("EMAIL_FROM", "RESEND_API_KEY"):
        os.environ.pop(k, None)


app = FastAPI(); app.include_router(auth_routes.router); app.include_router(email_routes.router); c = TestClient(app)
run = lambda co: asyncio.new_event_loop().run_until_complete(co)
H = {"Authorization": "Bearer " + core.make_token("e1")}


def test_verify_flow():
    run(mdb.users.insert_one({"id": "e1", "phone": "+919000000000"}))
    assert c.post("/api/auth/email/send-code", headers=H).status_code == 400  # no email yet
    c.put("/api/auth/me", headers=H, json={"email": "Me@Example.com", "name": "<b>Ravi</b> K"})
    assert c.post("/api/auth/email/send-code", headers=H).json() == {"sent": True}
    code = re.search(r"\b(\d{6})\b", sent[-1]["subject"]).group(1)
    assert sent[-1]["to"] == "me@example.com"
    assert c.post("/api/auth/email/verify", headers=H, json={"code": "000000" if code != "000000" else "111111"}).status_code == 400
    assert c.post("/api/auth/email/verify", headers=H, json={"code": code}).json() == {"verified": True}
    assert c.get("/api/auth/me", headers=H).json()["email_verified"] is True
    # a different address must be verified again
    assert c.put("/api/auth/me", headers=H, json={"email": "new@example.com"}).json()["email_verified"] is False


def test_order_email_only_to_verified_and_escaped():
    order = {"id": "abcdef1234", "user_id": "e1", "design_name": "Tee <script>", "size": "M", "qty": 1, "amount": 129900, "currency": "INR"}
    n = len(sent)
    assert run(email_routes.email_order(order, "shipped")) is False and len(sent) == n  # new address not verified
    run(mdb.users.update_one({"id": "e1"}, {"$set": {"email_verified": True}}))
    order["tracking_url"] = "https://track.example/AWB1"
    assert run(email_routes.email_order(order, "shipped")) is True
    m = sent[-1]
    assert m["subject"] == "Your order has shipped · #ABCDEF12" and "₹1,299.00" in m["text"]
    assert "<script>" not in m["html"] and "&lt;script&gt;" in m["html"] and "&lt;b&gt;Ravi" in m["html"]
    assert "https://track.example/AWB1" in m["html"]


def test_wrong_code_limit():
    run(mdb.users.update_one({"id": "e1"}, {"$set": {"email": "x@example.com"}, "$unset": {"email_verified": ""}}))
    assert c.post("/api/auth/email/send-code", headers=H).json() == {"sent": True}
    for _ in range(5):
        assert c.post("/api/auth/email/verify", headers=H, json={"code": "999999"}).status_code in (400,)
    assert c.post("/api/auth/email/verify", headers=H, json={"code": "999999"}).status_code == 429
