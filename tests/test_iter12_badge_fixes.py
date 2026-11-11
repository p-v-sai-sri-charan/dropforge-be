# Iteration 12: badge fixes - render 400 for undrawable characters, inactive-layout + saved design persistence
import os
import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/frontend/.env")
BASE_URL = os.environ["EXPO_PUBLIC_BACKEND_URL"].rstrip("/")
MSG = "This font can't show those characters"


@pytest.fixture(scope="module")
def fonts():
    r = requests.get(f"{BASE_URL}/api/fonts")
    assert r.status_code == 200
    return {f["label"]: f["id"] for f in r.json()}


@pytest.fixture(scope="module")
def badges():
    r = requests.get(f"{BASE_URL}/api/badges")
    assert r.status_code == 200
    return {b["id"]: b for b in r.json()}


def _render(**kw):
    return requests.post(f"{BASE_URL}/api/badges/render", json=kw)


# Fix 4: emoji-only words on templates WITH shapes -> 400
@pytest.mark.parametrize("tid", ["ring", "shield", "stamp", "vintage", "ribbon", "seal", "sunburst", "varsity"])
def test_emoji_only_400(tid, badges):
    b = badges[tid]
    r = _render(template=tid, top="😀😀", middle="🔥", bottom="🎉", font_id=b["font_id"], color="#111111", accent="#F2E8D5")
    assert r.status_code == 400, r.text
    assert MSG in r.json()["detail"]


def test_emoji_only_no_accent_400(badges):
    r = _render(template="ring", top="😀", middle="", bottom="", font_id=badges["ring"]["font_id"], color="#111111", accent=None)
    assert r.status_code == 400
    assert MSG in r.json()["detail"]


def test_mixed_emoji_and_letters_200(badges):
    r = _render(template="ring", top="😀 HI", middle="🔥", bottom="", font_id=badges["ring"]["font_id"], color="#111111", accent="#F2E8D5")
    assert r.status_code == 200
    assert r.json()["svg"].startswith("<svg")


def test_cjk_only_400(badges):
    r = _render(template="shield", top="漢字", middle="日本", bottom="", font_id=badges["shield"]["font_id"], color="#111111")
    assert r.status_code == 400
    assert MSG in r.json()["detail"]


def test_normal_render_still_200(badges):
    r = _render(template="stamp", top="OK", middle="FINE", bottom="GOODS", font_id=badges["stamp"]["font_id"], color="#111111", accent="#F2E8D5")
    assert r.status_code == 200
    assert "<path" in r.json()["svg"]


def test_previews_still_present(badges):
    assert len(badges) == 8
    for b in badges.values():
        assert b["preview"].startswith("<svg")
