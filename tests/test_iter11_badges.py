# Iteration 11: Badge templates (public list, render, admin toggle, design save with badge layer)
import os
import re
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
BASE_URL = (os.environ.get("EXPO_BACKEND_URL") or FENV.get("EXPO_PUBLIC_BACKEND_URL") or FENV["EXPO_PACKAGER_PROXY_URL"]).rstrip("/")
ENV = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
db = MongoClient(ENV["MONGO_URL"])[ENV["DB_NAME"]]
IDS = ["ring", "vintage", "ribbon", "shield", "stamp", "seal", "sunburst", "varsity"]
AH = admin_headers()
CREATED_USERS, CREATED_DESIGNS = [], []


def mk_user():
    uid = "TEST_" + uuid.uuid4().hex[:12]
    db.users.insert_one({"id": uid, "phone": "+1555" + str(uuid.uuid4().int)[:7], "created_at": time.time()})
    CREATED_USERS.append(uid)
    tok = jwt.encode({"sub": uid, "exp": int(time.time()) + 3600}, ENV["JWT_SECRET"], algorithm="HS256")
    return uid, {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="module", autouse=True)
def cleanup():
    yield
    for d, h in CREATED_DESIGNS:
        requests.delete(f"{BASE_URL}/api/designs/{d}", headers=h)
    for u in CREATED_USERS:
        db.users.delete_one({"id": u})
    # make sure every badge is back on
    for t in IDS:
        requests.patch(f"{BASE_URL}/api/admin/badges/{t}", headers=AH, json={"active": True})


@pytest.fixture(scope="module")
def badges():
    r = requests.get(f"{BASE_URL}/api/badges")
    assert r.status_code == 200
    return r.json()


def body(t, fid, **kw):
    b = {"template": t["id"], "font_id": fid, "color": "#111111", "accent": "#F2E8D5"}
    b.update({f["key"]: f["default"] for f in t["fields"]})
    b.update(kw)
    return b


# ---------- public list ----------
class TestList:
    def test_eight_templates(self, badges):
        assert [b["id"] for b in badges] == IDS

    def test_template_shape(self, badges):
        for b in badges:
            assert b["name"] and isinstance(b["name"], str)
            assert b["font_id"], b["id"]
            assert b["preview"].startswith("<svg") and 'viewBox="0 0 1000 1000"' in b["preview"]
            assert "active" not in b
            assert b["fields"] and all(set(f) == {"key", "label", "default", "max"} for f in b["fields"])
            assert all(len(f["default"]) <= f["max"] for f in b["fields"])
        assert [f["key"] for f in next(b for b in badges if b["id"] == "varsity")["fields"]] == ["top", "middle"]


# ---------- render ----------
class TestRender:
    def test_every_template_renders(self, badges):
        res = {}
        for t in badges:
            r = requests.post(f"{BASE_URL}/api/badges/render", json=body(t, t["font_id"]))
            res[t["id"]] = r.status_code
            assert r.status_code == 200, (t["id"], r.text)
            svg = r.json()["svg"]
            assert svg.startswith("<svg") and 'viewBox="0 0 1000 1000"' in svg
            assert re.search(r'<path d="M[^"]+" fill="#111111"', svg), t["id"]
        print(res)

    def test_colours_and_no_background(self, badges):
        t = badges[0]
        r = requests.post(f"{BASE_URL}/api/badges/render", json=body(t, t["font_id"], color="#FF0000", accent=None))
        assert r.status_code == 200
        svg = r.json()["svg"]
        assert "#FF0000" in svg and "#F2E8D5" not in svg and 'fill="none"' in svg

    def test_custom_words_change_svg(self, badges):
        t = badges[3]
        a = requests.post(f"{BASE_URL}/api/badges/render", json=body(t, t["font_id"])).json()["svg"]
        b = requests.post(f"{BASE_URL}/api/badges/render", json=body(t, t["font_id"], middle="TIGERS")).json()["svg"]
        assert a != b

    def test_all_empty_400(self, badges):
        t = badges[0]
        r = requests.post(f"{BASE_URL}/api/badges/render", json=body(t, t["font_id"], top=" ", middle="", bottom="  "))
        assert r.status_code == 400 and "words" in r.json()["detail"].lower()

    def test_unknown_template_404(self, badges):
        r = requests.post(f"{BASE_URL}/api/badges/render", json={"template": "nope", "font_id": badges[0]["font_id"], "color": "#111111", "middle": "X"})
        assert r.status_code == 404

    def test_unknown_font_404(self, badges):
        r = requests.post(f"{BASE_URL}/api/badges/render", json=body(badges[0], "no-such-font"))
        assert r.status_code == 404 and "font" in r.json()["detail"].lower()

    def test_inactive_font_404(self, badges):
        fonts = requests.get(f"{BASE_URL}/api/admin/fonts", headers=AH).json()
        inactive = [f for f in fonts if not f.get("active", True)]
        toggled = None
        if not inactive:
            # all fonts are template defaults; briefly switch the last one off (restored in finally). Run file with -p no:xdist.
            toggled = fonts[-1]
            assert requests.patch(f"{BASE_URL}/api/admin/fonts/{toggled['id']}", headers=AH, json={"active": False}).status_code == 200
            inactive = [toggled]
        try:
            r = requests.post(f"{BASE_URL}/api/badges/render", json=body(badges[0], inactive[0]["id"]))
            assert r.status_code == 404, r.text
            if toggled:  # templates whose default font is off fall back to an active font
                pub = requests.get(f"{BASE_URL}/api/badges").json()
                assert len(pub) == 8 and all(b["font_id"] and b["font_id"] != toggled["id"] for b in pub)
        finally:
            if toggled:
                requests.patch(f"{BASE_URL}/api/admin/fonts/{toggled['id']}", headers=AH, json={"active": True})

    def test_offensive_400(self, badges):
        _, h = mk_user()
        t = badges[1]
        r = requests.post(f"{BASE_URL}/api/badges/render", headers=h, json=body(t, t["font_id"], middle="F U C K"))
        assert r.status_code == 400
        assert r.json()["detail"].startswith("Sorry, that text can't be printed"), r.json()

    @pytest.mark.parametrize("c", ["red", "#12345", "#GGGGGG", "111111"])
    def test_bad_colour_422(self, badges, c):
        r = requests.post(f"{BASE_URL}/api/badges/render", json=body(badges[0], badges[0]["font_id"], color=c))
        assert r.status_code == 422
        r = requests.post(f"{BASE_URL}/api/badges/render", json=body(badges[0], badges[0]["font_id"], accent=c))
        assert r.status_code == 422

    def test_overlong_field_422(self, badges):
        r = requests.post(f"{BASE_URL}/api/badges/render", json=body(badges[0], badges[0]["font_id"], middle="X" * 17))
        assert r.status_code == 422


# ---------- admin ----------
class TestAdmin:
    def test_admin_requires_pin_and_phone(self):
        assert requests.get(f"{BASE_URL}/api/admin/badges").status_code == 403
        _, h = mk_user()
        assert requests.get(f"{BASE_URL}/api/admin/badges", headers={**h, "X-Admin-Pin": "2468"}).status_code == 403
        assert requests.get(f"{BASE_URL}/api/admin/badges", headers={"Authorization": AH["Authorization"], "X-Admin-Pin": "0000"}).status_code == 401
        assert requests.patch(f"{BASE_URL}/api/admin/badges/ring", json={"active": False}).status_code == 403

    def test_admin_list(self):
        r = requests.get(f"{BASE_URL}/api/admin/badges", headers=AH)
        assert r.status_code == 200
        d = r.json()
        assert [b["id"] for b in d] == IDS and all(b["active"] is True for b in d)

    def test_toggle_off_on(self, badges):
        t = next(b for b in badges if b["id"] == "stamp")
        try:
            r = requests.patch(f"{BASE_URL}/api/admin/badges/stamp", headers=AH, json={"active": False})
            assert r.status_code == 200 and r.json() == {"id": "stamp", "active": False}
            pub = [b["id"] for b in requests.get(f"{BASE_URL}/api/badges").json()]
            assert "stamp" not in pub and len(pub) == 7
            adm = {b["id"]: b["active"] for b in requests.get(f"{BASE_URL}/api/admin/badges", headers=AH).json()}
            assert adm["stamp"] is False and len(adm) == 8
            r = requests.post(f"{BASE_URL}/api/badges/render", json=body(t, t["font_id"]))
            assert r.status_code == 404
        finally:
            r = requests.patch(f"{BASE_URL}/api/admin/badges/stamp", headers=AH, json={"active": True})
            assert r.status_code == 200
        assert "stamp" in [b["id"] for b in requests.get(f"{BASE_URL}/api/badges").json()]
        assert requests.post(f"{BASE_URL}/api/badges/render", json=body(t, t["font_id"])).status_code == 200

    def test_toggle_unknown_404(self):
        assert requests.patch(f"{BASE_URL}/api/admin/badges/nope", headers=AH, json={"active": False}).status_code == 404

    def test_toggle_bad_body_422(self):
        assert requests.patch(f"{BASE_URL}/api/admin/badges/ring", headers=AH, json={}).status_code == 422


# ---------- designs ----------
class TestDesignBadge:
    def test_put_get_badge_layer(self, badges):
        _, h = mk_user()
        t = badges[0]
        meta = body(t, t["font_id"])
        svg = requests.post(f"{BASE_URL}/api/badges/render", json=meta).json()["svg"]
        did = "TEST_" + uuid.uuid4().hex[:10]
        CREATED_DESIGNS.append((did, h))
        layer = {"id": "l1", "kind": "badge", "src": svg, "x": 0.5, "y": 0.4, "scale": 0.6, "rotation": 0, "badge": meta}
        r = requests.put(f"{BASE_URL}/api/designs/{did}", headers=h, json={"name": "TEST_badge", "product": "tee", "color": "#FFFFFF", "front": [layer], "back": []})
        assert r.status_code == 200, r.text
        g = requests.get(f"{BASE_URL}/api/designs/{did}", headers=h)
        assert g.status_code == 200
        L = g.json()["front"][0]
        assert L["kind"] == "badge" and L["badge"]["template"] == "ring" and L["badge"]["middle"] == meta["middle"]
        assert L["badge"]["accent"] == "#F2E8D5" and L["src"].startswith("<svg")

    def test_put_badge_bad_meta_422(self, badges):
        _, h = mk_user()
        did = "TEST_" + uuid.uuid4().hex[:10]
        layer = {"id": "l1", "kind": "badge", "src": "<svg/>", "x": 0.5, "y": 0.4, "scale": 0.6, "rotation": 0,
                 "badge": {"template": "ring", "font_id": "x", "color": "red"}}
        r = requests.put(f"{BASE_URL}/api/designs/{did}", headers=h, json={"name": "TEST_b", "product": "tee", "color": "#FFFFFF", "front": [layer], "back": []})
        assert r.status_code == 422
