# Iteration 6: Cloudinary uploads (private authenticated / public), owner-checked proxy
import io
import os
import struct
import time
import uuid
import zlib

import cloudinary
import cloudinary.uploader
import jwt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

FE = dotenv_values("/app/frontend/.env")
BE = dotenv_values("/app/backend/.env")
BASE_URL = (os.environ.get("EXPO_BACKEND_URL") or FE["EXPO_PUBLIC_BACKEND_URL"]).strip('"').rstrip("/")
PHONE, CODE, PIN = "+15550001234", "246810", "2468"

db = MongoClient(BE["MONGO_URL"].strip('"'))[BE["DB_NAME"].strip('"')]
cloudinary.config(cloud_name=BE["CLOUDINARY_CLOUD_NAME"].strip('"'), api_key=BE["CLOUDINARY_API_KEY"].strip('"'),
                  api_secret=BE["CLOUDINARY_API_SECRET"].strip('"'), secure=True)


def png(w=4, h=4):
    raw = b"".join(b"\x00" + b"\xff\x00\x00\xff" * w for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9"
)
CREATED = []  # paths created by this run


@pytest.fixture(scope="module")
def H():
    r = requests.post(f"{BASE_URL}/api/auth/verify-otp", json={"phone": PHONE, "code": CODE})
    if r.status_code != 200:
        pytest.skip(f"login failed {r.status_code}")
    d = r.json()
    return {"Authorization": f"Bearer {d['token']}", "_token": d["token"], "_uid": d["user"]["id"] if "user" in d else None}


@pytest.fixture(scope="module")
def other():
    uid = f"TEST_{uuid.uuid4()}"
    db.users.insert_one({"id": uid, "phone": "+1555TEST", "created_at": "2026-01-01T00:00:00+00:00"})
    tok = jwt.encode({"sub": uid, "exp": int(time.time()) + 3600}, BE["JWT_SECRET"].strip('"'), algorithm="HS256")
    yield {"Authorization": f"Bearer {tok}"}
    db.users.delete_one({"id": uid})


@pytest.fixture(scope="module", autouse=True)
def cleanup():
    yield
    for p in CREATED:
        rec = db.files.find_one({"path": p})
        if rec and rec.get("provider") == "cloudinary":
            try:
                cloudinary.uploader.destroy(rec["public_id"], type=rec["delivery_type"], resource_type="image", invalidate=True)
            except Exception as e:
                print("cld cleanup failed", e)
        if rec:
            db.files.delete_one({"id": rec["id"]})


def up(H, data=None, name="a.png", ct="image/png", url="/api/upload", headers=None):
    h = headers if headers is not None else {"Authorization": H["Authorization"]}
    r = requests.post(f"{BASE_URL}{url}", files={"file": (name, io.BytesIO(data or png()), ct)}, headers=h, timeout=60)
    if r.status_code == 200:
        CREATED.append(r.json()["path"])
    return r


# --- Private uploads to Cloudinary (authenticated delivery) ---
class TestPrivateUpload:
    def test_png_upload_record(self, H):
        r = up(H)
        assert r.status_code == 200, r.text
        url, path = r.json()["url"], r.json()["path"]
        rec = db.files.find_one({"path": path}, {"_id": 0})
        assert url.startswith(f"/api/files/cld/dropforge/private/{rec['owner_id']}/") and url.endswith(".png")
        assert rec["provider"] == "cloudinary" and rec["delivery_type"] == "authenticated" and rec["public"] is False
        assert "res.cloudinary.com" in rec["cloud_url"]
        # cloudinary URL with the signature removed must not be reachable
        import re
        unsigned = re.sub(r"/s--[^/]+--", "", rec["cloud_url"])
        assert unsigned != rec["cloud_url"]
        assert requests.get(unsigned, timeout=30).status_code in (401, 403, 404)
        TestPrivateUpload.url = url

    def test_jpeg_upload(self, H):
        r = up(H, JPEG, "a.jpg", "image/jpeg")
        assert r.status_code == 200, r.text
        assert "/private/" in r.json()["url"]
        g = requests.get(f"{BASE_URL}{r.json()['url']}", headers={"Authorization": H["Authorization"]}, timeout=60)
        assert g.status_code == 200 and g.content[:3] == b"\xff\xd8\xff" and g.headers["content-type"] == "image/jpeg"

    def test_owner_bearer(self, H):
        g = requests.get(f"{BASE_URL}{self.url}", headers={"Authorization": H["Authorization"]}, timeout=60)
        assert g.status_code == 200 and g.content[:4] == b"\x89PNG"

    def test_owner_query_token(self, H):
        g = requests.get(f"{BASE_URL}{self.url}?token={H['_token']}", timeout=60)
        assert g.status_code == 200 and g.content[:4] == b"\x89PNG"

    def test_anonymous_404(self):
        assert requests.get(f"{BASE_URL}{self.url}", timeout=30).status_code == 404

    def test_other_user_404(self, other):
        assert requests.get(f"{BASE_URL}{self.url}", headers=other, timeout=30).status_code == 404


# --- Public uploads ---
class TestPublicUpload:
    def test_public_forbidden_non_creator(self, H):
        r = up(H, url="/api/upload?public=true")
        assert r.status_code == 403

    def test_admin_upload_public(self, H):
        r = up(H, url="/api/admin/upload", headers={"X-Admin-Pin": PIN})
        assert r.status_code == 200, r.text
        url = r.json()["url"]
        assert url.startswith("/api/files/cld/dropforge/public/admin/")
        rec = db.files.find_one({"path": r.json()["path"]}, {"_id": 0})
        assert rec["delivery_type"] == "upload" and rec["public"] is True and rec["provider"] == "cloudinary"
        g = requests.get(f"{BASE_URL}{url}", timeout=60)
        assert g.status_code == 200 and g.content[:4] == b"\x89PNG"

    def test_admin_upload_bad_pin(self, H):
        r = up(H, url="/api/admin/upload", headers={"X-Admin-Pin": "0000"})
        assert r.status_code in (401, 403)


# --- Validation ---
class TestValidation:
    def test_text_rejected(self, H):
        assert up(H, b"hello world", "a.txt", "text/plain").status_code == 400

    def test_spoofed_png_rejected(self, H):
        assert up(H, b"not really a png at all", "a.png", "image/png").status_code == 400

    def test_too_large_413(self, H):
        big = png()[:8] + b"\x00" * (8 * 1024 * 1024 + 10)
        r = up(H, big)
        assert r.status_code == 413, r.status_code

    def test_no_auth_401(self):
        r = requests.post(f"{BASE_URL}/api/upload", files={"file": ("a.png", io.BytesIO(png()), "image/png")}, timeout=30)
        assert r.status_code == 401


# --- Unknown paths ---
class TestUnknown:
    def test_unknown_path_404(self):
        assert requests.get(f"{BASE_URL}/api/files/cld/dropforge/private/x/nope.png", timeout=30).status_code == 404


# --- Share flow: private cloudinary layer + snapshot become publicly readable after share ---
class TestShareFlow:
    def test_share_makes_files_public(self, H):
        lay = up(H).json()["url"]
        snap = up(H).json()["url"]
        assert requests.get(f"{BASE_URL}{lay}", timeout=30).status_code == 404
        did = f"TEST_{uuid.uuid4()}"
        body = {"name": "TEST_CLD_SHARE", "product": "hoodie", "color": "#FFFFFF", "thumb": snap,
                "front": [{"id": "l1", "kind": "image", "src": lay, "x": 0.5, "y": 0.5, "scale": 1, "rotation": 0}], "back": []}
        r = requests.put(f"{BASE_URL}/api/designs/{did}", json=body, headers={"Authorization": H["Authorization"]}, timeout=30)
        assert r.status_code == 200, r.text
        try:
            s = requests.post(f"{BASE_URL}/api/shares", json={"design_id": did, "front_image": snap},
                              headers={"Authorization": H["Authorization"]}, timeout=30)
            assert s.status_code == 200, s.text
            slug = s.json()["slug"]
            for u in (lay, snap):
                g = requests.get(f"{BASE_URL}{u}", timeout=60)
                assert g.status_code == 200 and g.content[:4] == b"\x89PNG"
            page = requests.get(s.json()["url"], timeout=30)
            assert page.status_code == 200 and snap in page.text
            db.shares.delete_one({"slug": slug})
        finally:
            requests.delete(f"{BASE_URL}/api/designs/{did}", headers={"Authorization": H["Authorization"]}, timeout=30)
