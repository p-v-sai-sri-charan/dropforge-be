import os
import pytest
import requests

BASE_URL = os.environ.get('EXPO_PUBLIC_BACKEND_URL').rstrip('/')
PIN = "2468"


@pytest.fixture
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


@pytest.fixture(autouse=True, scope="module")
def reset_after():
    yield
    requests.post(f"{BASE_URL}/api/config/reset", headers={"X-Admin-Pin": PIN})


# Config: GET defaults
class TestConfig:
    def test_reset_then_get_default(self, s):
        r = s.post(f"{BASE_URL}/api/config/reset", headers={"X-Admin-Pin": PIN})
        assert r.status_code == 200
        r = s.get(f"{BASE_URL}/api/config")
        assert r.status_code == 200
        d = r.json()
        assert d["max_layers"] == 6
        assert d["max_scale"] == 2.5
        assert d["front"] == {"x": 0.3, "y": 0.24, "w": 0.4, "h": 0.46}
        assert len(d["colors"]) == 10
        assert "_id" not in d

    def _cfg(self, s):
        return s.get(f"{BASE_URL}/api/config").json()

    def test_put_no_pin_401(self, s):
        r = s.put(f"{BASE_URL}/api/config", json=self._cfg(s))
        assert r.status_code == 401

    def test_put_wrong_pin_401(self, s):
        r = s.put(f"{BASE_URL}/api/config", json=self._cfg(s), headers={"X-Admin-Pin": "0000"})
        assert r.status_code == 401

    def test_put_invalid_x_plus_w(self, s):
        c = self._cfg(s)
        c["front"] = {"x": 0.7, "y": 0.2, "w": 0.5, "h": 0.4}
        r = s.put(f"{BASE_URL}/api/config", json=c, headers={"X-Admin-Pin": PIN})
        assert r.status_code == 422

    def test_put_valid_persists_and_reset(self, s):
        c = self._cfg(s)
        c["max_layers"] = 3
        c["colors"] = ["#FFFFFF", "#111111"]
        r = s.put(f"{BASE_URL}/api/config", json=c, headers={"X-Admin-Pin": PIN})
        assert r.status_code == 200
        g = self._cfg(s)
        assert g["max_layers"] == 3 and g["colors"] == ["#FFFFFF", "#111111"]
        r = s.post(f"{BASE_URL}/api/config/reset", headers={"X-Admin-Pin": PIN})
        assert r.status_code == 200 and r.json()["max_layers"] == 6
        assert self._cfg(s)["max_layers"] == 6

    def test_reset_no_pin_401(self, s):
        assert s.post(f"{BASE_URL}/api/config/reset").status_code == 401


# Admin PIN verify
class TestAdmin:
    def test_verify_ok(self, s):
        r = s.post(f"{BASE_URL}/api/admin/verify", json={"pin": PIN})
        assert r.status_code == 200 and r.json()["ok"] is True

    def test_verify_wrong(self, s):
        assert s.post(f"{BASE_URL}/api/admin/verify", json={"pin": "1111"}).status_code == 401


# AI (Claude)
class TestAI:
    def test_ideas(self, s):
        r = s.post(f"{BASE_URL}/api/ai/ideas", json={"shirt_color": "#111111", "theme": "space"}, timeout=90)
        assert r.status_code == 200, r.text
        ideas = r.json()["ideas"]
        assert isinstance(ideas, list) and 1 <= len(ideas) <= 6
        assert all(isinstance(i, str) and i for i in ideas)

    def test_generate_svg(self, s):
        r = s.post(f"{BASE_URL}/api/ai/generate-svg", json={"prompt": "retro sunset with palm tree", "shirt_color": "#FFFFFF"}, timeout=120)
        assert r.status_code == 200, r.text
        svg = r.json()["svg"]
        assert svg.startswith("<svg") and svg.endswith("</svg>")

    def test_generate_svg_short_prompt_422(self, s):
        assert s.post(f"{BASE_URL}/api/ai/generate-svg", json={"prompt": "a"}).status_code == 422
