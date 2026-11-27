import os, sys, asyncio
from datetime import date
from mongomock_motor import AsyncMongoMockClient
import core
mdb = AsyncMongoMockClient()["t2"]; core.db = mdb
import auth_routes; auth_routes.db = mdb

import pytest


@pytest.fixture(autouse=True, scope="module")
def _this_modules_db():
    """Each test module has its own in-memory database; point the shared sign-in code (core.db) at it."""
    core.db = mdb
    for m in (auth_routes,):
        m.db = mdb

from fastapi import FastAPI
from fastapi.testclient import TestClient
app = FastAPI(); app.include_router(auth_routes.router); c = TestClient(app)
run = lambda co: asyncio.new_event_loop().run_until_complete(co)
run(mdb.users.insert_one({"id": "u1", "phone": "+919876543210"}))
H = {"Authorization": "Bearer " + core.make_token("u1")}

def test_me_has_profile_and_creator_status():
    me = c.get("/api/auth/me", headers=H).json()
    assert me["phone"] == "+919876543210" and me["creator_status"] is None
    assert me["name"] is None and me["email"] is None and me["dob"] is None and me["gender"] is None

def test_update_and_clear():
    r = c.put("/api/auth/me", headers=H, json={"name": "  Asha   Rao ", "email": " Asha@Example.COM ", "dob": "1999-04-12", "gender": "female"})
    assert r.status_code == 200, r.text
    me = r.json()
    assert (me["name"], me["email"], me["dob"], me["gender"]) == ("Asha Rao", "asha@example.com", "1999-04-12", "female")
    # partial update leaves the rest alone; empty string clears
    me = c.put("/api/auth/me", headers=H, json={"email": ""}).json()
    assert me["email"] is None and me["name"] == "Asha Rao"
    assert "email" not in run(mdb.users.find_one({"id": "u1"}))

def test_validation():
    young = date(date.today().year - 10, 1, 1).isoformat()
    for body, msg in [({"email": "nope"}, "valid email"), ({"dob": "2999-01-01"}, "real date"), ({"dob": young}, "at least 13"),
                      ({"dob": "12/04/1999"}, "DD/MM/YYYY"), ({"gender": "robot"}, ""), ({"name": "A"}, "too short")]:
        r = c.put("/api/auth/me", headers=H, json=body)
        assert r.status_code == 422 and msg in r.text, (body, r.text)
    assert c.put("/api/auth/me", json={"name": "X Y"}).status_code == 401

def test_creator_status_flows_through():
    run(mdb.creators.insert_one({"user_id": "u1", "status": "pending"}))
    assert c.get("/api/auth/me", headers=H).json()["creator_status"] == "pending"
    run(mdb.creators.update_one({"user_id": "u1"}, {"$set": {"status": "approved"}}))
    assert c.get("/api/auth/me", headers=H).json()["creator_status"] == "approved"
