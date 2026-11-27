import asyncio
from types import SimpleNamespace
from mongomock_motor import AsyncMongoMockClient
import core
mdb = AsyncMongoMockClient()["t_login"]
import auth_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient
from twilio.base.exceptions import TwilioRestException
import pytest

sent = []


class FakeVerify:
    def __init__(self, fail=None):
        self.fail = fail
        self.verifications = SimpleNamespace(create=self._send)
        self.verification_checks = SimpleNamespace(create=lambda to, code: SimpleNamespace(status="approved" if code == "123456" else "pending"))

    def _send(self, to, channel):
        if self.fail:
            raise TwilioRestException(400, "uri", msg="blocked", code=self.fail)
        sent.append(to)
        return SimpleNamespace(sid="VE1", status="pending")


@pytest.fixture(autouse=True, scope="module")
def _this_modules_db():
    core.db = mdb
    auth_routes.db = mdb
    auth_routes.sms_enabled = lambda: True


app = FastAPI(); app.include_router(auth_routes.router); c = TestClient(app)
run = lambda co: asyncio.new_event_loop().run_until_complete(co)


def test_country_code_typed_twice_is_rejected_before_twilio(monkeypatch):
    monkeypatch.setattr(auth_routes, "_twilio", lambda: FakeVerify())
    r = c.post("/api/auth/send-otp", json={"phone": "+91919876543210"})
    assert r.status_code == 400 and "isn't a valid phone number" in r.json()["detail"] and sent == []
    assert c.post("/api/auth/send-otp", json={"phone": "+912240000000"}).json()["detail"].startswith("This looks like a landline")
    r = c.post("/api/auth/send-otp", json={"phone": "+91 098765-43210"})  # stray trunk 0 is cleaned up
    assert r.status_code == 200 and r.json()["phone"] == "+919876543210" and sent == ["+919876543210"]


def test_twilio_refusal_is_explained_and_not_counted(monkeypatch):
    monkeypatch.setattr(auth_routes, "_twilio", lambda: FakeVerify(fail=60410))
    before = run(mdb.otp_sends.count_documents({}))
    r = c.post("/api/auth/send-otp", json={"phone": "+919812345678"})
    assert r.status_code == 400 and "temporarily blocked" in r.json()["detail"]
    assert run(mdb.otp_sends.count_documents({})) == before


def test_old_account_saved_with_trunk_zero_is_kept(monkeypatch):
    monkeypatch.setattr(auth_routes, "_twilio", lambda: FakeVerify())
    run(mdb.users.insert_one({"id": "old", "phone": "+9109811111111"}))
    r = c.post("/api/auth/verify-otp", json={"phone": "+9109811111111", "code": "123456"}).json()
    assert r["user"]["id"] == "old" and r["user"]["phone"] == "+919811111111"
