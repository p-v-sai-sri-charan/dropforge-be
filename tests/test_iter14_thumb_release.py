# Iteration 14: PUT /api/designs releases the replaced thumb unless an order/share still references it; 3D save regression
import io
import os
import time
import uuid
from pathlib import Path

import jwt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

FENV = dotenv_values("/app/frontend/.env")
BASE_URL = (os.environ.get("EXPO_BACKEND_URL") or FENV.get("EXPO_PUBLIC_BACKEND_URL") or FENV["EXPO_BACKEND_URL"]).rstrip("/")
API = f"{BASE_URL}/api"
ENV = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
DB = MongoClient(ENV["MONGO_URL"])[ENV["DB_NAME"]]
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.fixture(scope="module")
def user():
    uid = "TEST_" + uuid.uuid4().hex[:12]
    DB.users.insert_one({"id": uid, "phone": "+1555001" + str(int(time.time()))[-4:], "created_at": time.time()})
    tok = jwt.encode({"sub": uid, "exp": int(time.time()) + 3600}, ENV["JWT_SECRET"], algorithm="HS256")
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {tok}"
    s.uid = uid
    s.created, s.uploads, s.orders = [], [], []
    yield s
    for oid in s.orders:
        DB.orders.delete_one({"id": oid})
    DB.shares.delete_many({"user_id": uid})
    for did in s.created:
        s.delete(f"{API}/designs/{did}")
    # release anything left over that we uploaded
    for u in s.uploads:
        rec = DB.files.find_one({"path": u[len("/api/files/"):], "is_deleted": False})
        if rec and not any(c.find_one({"$or": [{"design_snapshot.thumb": u}, {"thumb": u}]}) for c in (DB.orders, DB.designs)):
            DB.files.update_one({"_id": rec["_id"]}, {"$set": {"is_deleted": True}})
    DB.users.delete_one({"id": uid})


def _upload(s):
    r = s.post(f"{API}/upload", files={"file": ("t.png", io.BytesIO(PNG), "image/png")})
    assert r.status_code == 200, r.text
    j = r.json()
    u = j.get("url") or j.get("src") or j.get("path")
    assert u.startswith("/api/files/")
    s.uploads.append(u)
    return u


def _rec(u):
    return DB.files.find_one({"path": u[len("/api/files/"):]}, {"_id": 0})


def _design(product, thumb):
    return {"name": f"TEST_{product}", "product": product, "color": "#222222", "thumb": thumb,
            "front": [{"id": "t1", "kind": "svg", "src": "<svg xmlns='http://www.w3.org/2000/svg'></svg>", "x": 0.5, "y": 0.5, "scale": 1, "rotation": 0}],
            "back": []}


def _put(s, did, product, thumb):
    r = s.put(f"{API}/designs/{did}", json=_design(product, thumb))
    assert r.status_code == 200, r.text
    if did not in s.created:
        s.created.append(did)
    return r.json()


@pytest.mark.parametrize("product", ["sweatshirt", "hoodie"])
def test_resave_releases_old_thumb(user, product):
    did = "TEST_" + uuid.uuid4().hex[:10]
    t1, t2 = _upload(user), _upload(user)
    _put(user, did, product, t1)
    assert _rec(t1)["is_deleted"] is False
    _put(user, did, product, t2)
    assert user.get(f"{API}/designs/{did}").json()["thumb"] == t2
    assert _rec(t1)["is_deleted"] is True, _rec(t1)
    assert _rec(t2)["is_deleted"] is False
    assert user.get(f"{BASE_URL}{t2}").status_code == 200  # private file, owner can read


def test_resave_same_thumb_keeps_file(user):
    did = "TEST_" + uuid.uuid4().hex[:10]
    t1 = _upload(user)
    _put(user, did, "tee3d", t1)
    _put(user, did, "tee3d", t1)
    assert _rec(t1)["is_deleted"] is False


def test_resave_keeps_thumb_referenced_by_share(user):
    did = "TEST_" + uuid.uuid4().hex[:10]
    t1, t2 = _upload(user), _upload(user)
    _put(user, did, "croptop", t1)
    s = user.post(f"{API}/shares", json={"design_id": did})
    assert s.status_code == 200, s.text
    _put(user, did, "croptop", t2)
    assert _rec(t1)["is_deleted"] is False, "share snapshot still uses old thumb"


def test_resave_keeps_thumb_referenced_by_order(user):
    did = "TEST_" + uuid.uuid4().hex[:10]
    t1, t2 = _upload(user), _upload(user)
    _put(user, did, "sleeveless", t1)
    oid = "TEST_" + uuid.uuid4().hex[:10]
    DB.orders.insert_one({"id": oid, "user_id": user.uid, "design_id": did, "status": "created",
                          "design_snapshot": {"thumb": t1, "front": [], "back": []}})
    user.orders.append(oid)
    _put(user, did, "sleeveless", t2)
    assert _rec(t1)["is_deleted"] is False, "order snapshot still uses old thumb"


def test_resave_keeps_thumb_used_by_other_design(user):
    d1, d2 = "TEST_" + uuid.uuid4().hex[:10], "TEST_" + uuid.uuid4().hex[:10]
    t1, t2 = _upload(user), _upload(user)
    _put(user, d1, "sweatshirt", t1)
    _put(user, d2, "sweatshirt", t1)
    _put(user, d1, "sweatshirt", t2)
    assert _rec(t1)["is_deleted"] is False


def test_put_cannot_hijack_other_users_design(user):
    did = "TEST_" + uuid.uuid4().hex[:10]
    t1 = _upload(user)
    _put(user, did, "hoodie", t1)
    uid2 = "TEST_" + uuid.uuid4().hex[:12]
    DB.users.insert_one({"id": uid2, "phone": "+15550027777", "created_at": time.time()})
    try:
        tok = jwt.encode({"sub": uid2, "exp": int(time.time()) + 3600}, ENV["JWT_SECRET"], algorithm="HS256")
        r = requests.put(f"{API}/designs/{did}", json=_design("hoodie", None), headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 409
        assert _rec(t1)["is_deleted"] is False
    finally:
        DB.users.delete_one({"id": uid2})
