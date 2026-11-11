# Iteration 13: four new 3D garments (tee3d, sleeveless, sweatshirt, croptop) - models, config, designs, quotes, shares
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

from conftest import admin_headers

FENV = dotenv_values("/app/frontend/.env")
BASE_URL = (os.environ.get("EXPO_BACKEND_URL") or FENV.get("EXPO_PUBLIC_BACKEND_URL") or FENV["EXPO_BACKEND_URL"]).rstrip("/")
API = f"{BASE_URL}/api"
ENV = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
DB = MongoClient(ENV["MONGO_URL"])[ENV["DB_NAME"]]
ALL = {"tee", "hoodie", "tote", "tee3d", "sleeveless", "sweatshirt", "croptop"}
NEW = ["tee3d", "sleeveless", "sweatshirt", "croptop"]
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.fixture(scope="module")
def user():
    uid = "TEST_" + uuid.uuid4().hex[:12]
    DB.users.insert_one({"id": uid, "phone": "+1555000" + str(int(time.time()))[-4:], "created_at": time.time()})
    tok = jwt.encode({"sub": uid, "exp": int(time.time()) + 3600}, ENV["JWT_SECRET"], algorithm="HS256")
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {tok}"
    s.created = []
    yield s
    for did in s.created:
        s.delete(f"{API}/designs/{did}")
    DB.shares.delete_many({"user_id": uid})
    DB.users.delete_one({"id": uid})


# ---- GLB models ----
@pytest.mark.parametrize("name", ["hoodie"] + NEW)
def test_model_glb(name):
    r = requests.get(f"{API}/models/{name}.glb")
    assert r.status_code == 200
    assert r.content[:4] == b"glTF"
    assert "gltf-binary" in r.headers.get("content-type", "")


@pytest.mark.parametrize("path", ["tee.glb", "unknown.glb", "..%2Fserver.glb", "..%2F..%2Fbackend%2Fserver.glb", "../static/hoodie.glb", "hoodie"])
def test_model_bad_names(path):
    r = requests.get(f"{API}/models/{path}")
    assert r.status_code == 404, (path, r.status_code, r.text[:80])
    assert b"glTF" not in r.content[:4] or path == "../static/hoodie.glb"


# ---- config ----
def test_config_has_all_products():
    c = requests.get(f"{API}/config").json()
    assert set(c["products"]) == ALL
    for k, p in c["products"].items():
        assert {"front", "back", "prices"} <= set(p)
        assert set(p["prices"]) == {"INR", "USD", "GBP", "EUR"}


def test_legacy_config_keeps_admin_values():
    saved = DB.settings.find_one({"key": "print_config"}, {"_id": 0})
    c = requests.get(f"{API}/config").json()
    if saved and set(saved.get("products", {})) == {"tee", "hoodie", "tote"}:
        for k in ("tee", "hoodie", "tote"):
            assert c["products"][k] == saved["products"][k], k
        assert c["max_layers"] == saved["max_layers"]
        assert c["products"]["tee3d"]["prices"]["INR"] == 899
        assert c["products"]["sweatshirt"]["prices"]["INR"] == 1599
    else:
        pytest.skip("saved config is not legacy-3 format")


def test_put_config_requires_all_products_and_roundtrip():
    original = DB.settings.find_one({"key": "print_config"}, {"_id": 0})
    cfg = requests.get(f"{API}/config").json()
    h = admin_headers()
    try:
        partial = {**cfg, "products": {k: cfg["products"][k] for k in ("tee", "hoodie", "tote")}}
        r = requests.put(f"{API}/config", json=partial, headers=h)
        assert r.status_code in (400, 422), r.text
        # non-admin / no pin blocked
        assert requests.put(f"{API}/config", json=cfg).status_code in (401, 403)
        new = {**cfg, "products": {**cfg["products"], "croptop": {**cfg["products"]["croptop"], "prices": {**cfg["products"]["croptop"]["prices"], "INR": 777}}}}
        r = requests.put(f"{API}/config", json=new, headers=h)
        assert r.status_code == 200, r.text
        assert requests.get(f"{API}/config").json()["products"]["croptop"]["prices"]["INR"] == 777
    finally:
        if original:
            DB.settings.replace_one({"key": "print_config"}, original, upsert=True)
        else:
            DB.settings.delete_one({"key": "print_config"})
    assert DB.settings.find_one({"key": "print_config"}, {"_id": 0}) == original


# ---- designs + quotes ----
def _design(product, thumb=None):
    return {"name": f"TEST_{product}", "product": product, "color": "#111111", "thumb": thumb,
            "front": [{"id": "t1", "kind": "svg", "src": "<svg xmlns='http://www.w3.org/2000/svg'></svg>", "x": 0.5, "y": 0.5, "scale": 1, "rotation": 0}],
            "back": []}


@pytest.mark.parametrize("product", NEW)
def test_design_put_get_and_quote(user, product):
    did = "TEST_" + uuid.uuid4().hex[:10]
    r = user.put(f"{API}/designs/{did}", json=_design(product))
    assert r.status_code == 200, r.text
    user.created.append(did)
    assert user.get(f"{API}/designs/{did}").json()["product"] == product
    price = requests.get(f"{API}/config").json()["products"][product]["prices"]["INR"]
    for size in ["XS", "XXL"]:
        q = user.post(f"{API}/orders/quote", json={"design_id": did, "size": size, "qty": 2, "currency": "INR"})
        assert q.status_code == 200, q.text
        assert q.json()["subtotal"] == int(round(price * 100)) * 2
    q = user.post(f"{API}/orders/quote", json={"design_id": did, "size": "ONE SIZE", "qty": 1, "currency": "INR"})
    assert q.status_code == 400


def test_design_rejects_bad_product(user):
    r = user.put(f"{API}/designs/TEST_bad{uuid.uuid4().hex[:6]}", json=_design("jacket"))
    assert r.status_code in (400, 422)


def test_share_page_3d_shows_rendered_images(user):
    ups = []
    for _ in range(2):
        r = user.post(f"{API}/upload", files={"file": ("a.png", io.BytesIO(PNG), "image/png")})
        assert r.status_code == 200, r.text
        j = r.json()
        ups.append(j.get("url") or j.get("src") or j.get("path"))
    assert all(u and u.startswith("/api/files/") for u in ups), ups
    did = "TEST_" + uuid.uuid4().hex[:10]
    assert user.put(f"{API}/designs/{did}", json=_design("sweatshirt")).status_code == 200
    user.created.append(did)
    s = user.post(f"{API}/shares", json={"design_id": did, "front_image": ups[0], "back_image": ups[1]})
    assert s.status_code == 200, s.text
    html = requests.get(f"{API}/s/{s.json()['slug']}").text
    assert ups[0] in html and ups[1] in html
    assert 'class="shot"' in html and "SWEATSHIRT" in html
    assert "<option selected>M</option>" in html and "XXL" in html
    # image must be publicly reachable for share viewers
    assert requests.get(f"{BASE_URL}{ups[0]}").status_code == 200
