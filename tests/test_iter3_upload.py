# Iteration 3: upload MIME restrictions (SVG blocked) + regression for allowed PNG
import io
import os

import pytest
import requests
from dotenv import dotenv_values

FE = dotenv_values("/app/frontend/.env")
BASE_URL = (os.environ.get("EXPO_BACKEND_URL") or FE["EXPO_PUBLIC_BACKEND_URL"]).strip('"').rstrip("/")
PHONE, CODE = "+15550001234", "246810"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture(scope="module")
def H():
    r = requests.post(f"{BASE_URL}/api/auth/verify-otp", json={"phone": PHONE, "code": CODE})
    if r.status_code != 200:
        pytest.skip(f"login failed {r.status_code}")
    return {"Authorization": f"Bearer {r.json()['token']}"}


# --- Upload MIME validation ---
class TestUploadMime:
    def test_svg_rejected(self, H):
        r = requests.post(f"{BASE_URL}/api/upload", files={"file": ("a.svg", io.BytesIO(SVG), "image/svg+xml")}, headers=H)
        assert r.status_code == 400
        assert "PNG" in r.json()["detail"]

    def test_text_rejected(self, H):
        r = requests.post(f"{BASE_URL}/api/upload", files={"file": ("a.txt", b"hi", "text/plain")}, headers=H)
        assert r.status_code == 400

    def test_png_allowed(self, H):
        r = requests.post(f"{BASE_URL}/api/upload", files={"file": ("a.png", io.BytesIO(PNG), "image/png")}, headers=H)
        assert r.status_code == 200
        url = r.json()["url"]
        g = requests.get(f"{BASE_URL}{url}" if url.startswith("/") else url, headers=H)
        assert g.status_code == 200 and g.content[:4] == b"\x89PNG"

    def test_no_auth_rejected(self):
        r = requests.post(f"{BASE_URL}/api/upload", files={"file": ("a.svg", io.BytesIO(SVG), "image/svg+xml")})
        assert r.status_code in (401, 403)
