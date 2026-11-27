import asyncio, io, os, time
from datetime import datetime, timedelta, timezone
from mongomock_motor import AsyncMongoMockClient
from PIL import Image
import core
mdb = AsyncMongoMockClient()["t_storage"]
import files_routes, storage
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


class FakeR2:
    """Just enough of the S3 API: buckets are dicts of key -> (bytes, when)."""
    def __init__(self):
        self.b = {}

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl):
        self.b.setdefault(Bucket, {})[Key] = (Body, datetime.now(timezone.utc))

    def upload_fileobj(self, f, Bucket, Key, ExtraArgs=None):
        self.b.setdefault(Bucket, {})[Key] = (f.read(), datetime.now(timezone.utc))

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.b[Bucket][Key][0])}

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.b.get(Bucket, {}).pop(o["Key"], None)

    def list_objects_v2(self, Bucket, Prefix, **kw):
        return {"Contents": [{"Key": k, "Size": len(v[0]), "LastModified": v[1]} for k, v in self.b.get(Bucket, {}).items() if k.startswith(Prefix)]}


fake = FakeR2()


@pytest.fixture(autouse=True, scope="module")
def _this_modules_db():
    core.db = mdb
    files_routes.db = mdb
    files_routes.cloudinary_ready = lambda: False
    storage._client = fake
    env = {"R2_ACCOUNT_ID": "acc", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s", "R2_BUCKET": "priv"}
    old = {k: os.environ.get(k) for k in [*env, "R2_PUBLIC_BUCKET", "R2_PUBLIC_URL", "STORAGE_BACKEND"]}
    os.environ.update(env)
    yield
    for k, v in old.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


app = FastAPI(); app.include_router(files_routes.router); c = TestClient(app, follow_redirects=False)
H = lambda uid: {"Authorization": "Bearer " + core.make_token(uid)}
run = lambda co: asyncio.new_event_loop().run_until_complete(co)


def png(w=1200, h=800):
    out = io.BytesIO(); Image.new("RGBA", (w, h), (23, 63, 173, 255)).save(out, "PNG"); return out.getvalue()


def test_private_upload_owner_only_and_thumbs():
    run(mdb.users.insert_many([{"id": "u1", "phone": "+911"}, {"id": "u2", "phone": "+912"}]))
    r = c.post("/api/upload", headers=H("u1"), files={"file": ("a.png", png(), "image/png")}).json()
    assert r["path"].startswith("r2/") and r["path"].endswith(".png")
    rec = run(mdb.files.find_one({"path": r["path"]}))
    assert rec["bucket"] == "priv" and rec["key"].startswith("private/u1/")
    assert c.get(r["url"], headers=H("u2")).status_code == 404
    full = c.get(r["url"], headers=H("u1"))
    assert full.status_code == 200 and full.content == fake.b["priv"][rec["key"]][0]
    t = c.get(r["url"] + "?w=300", headers=H("u1"))
    assert t.headers["content-type"] == "image/webp" and Image.open(io.BytesIO(t.content)).size == (480, 320)
    assert run(mdb.files.find_one({"path": r["path"]}))["thumbs"] == [480]
    assert c.get(r["url"] + "?w=300", headers=H("u1")).content == t.content  # served from the cached variant
    # deleting removes the original and its variants
    assert run(files_routes.release_files([r["url"]])) == 1
    assert not [k for k in fake.b["priv"] if rec["id"] in k]


def test_public_photos_redirect_to_cdn():
    os.environ.update({"R2_PUBLIC_BUCKET": "pub", "R2_PUBLIC_URL": "https://cdn.example.in"})
    run(mdb.creators.insert_one({"user_id": "u1", "status": "approved"}))
    r = c.post("/api/upload?public=true", headers=H("u1"), files={"file": ("p.png", png(600, 600), "image/png")}).json()
    rec = run(mdb.files.find_one({"path": r["path"]}))
    assert rec["bucket"] == "pub" and rec["key"] in fake.b["pub"]
    g = c.get(r["url"])
    assert g.status_code == 302 and g.headers["location"] == f"https://cdn.example.in/{rec['key']}"
    assert c.get(r["url"] + "?w=240").status_code == 200  # first time: made and stored
    g = c.get(r["url"] + "?w=240")
    assert g.status_code == 302 and g.headers["location"].endswith(f"thumbs/240/{rec['key']}.webp")


def test_offsite_backup(tmp_path):
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("backup_to_r2", os.path.join(os.path.dirname(__file__), "..", "scripts", "backup_to_r2.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    f = tmp_path / "dropforge-Mon.archive.gz"; f.write_bytes(b"dump")
    old = time.time() - 3600; os.utime(f, (old, old))
    (tmp_path / "notes.txt").write_text("x")
    up = mod.run_once(str(tmp_path), 30)
    assert len(up) == 1 and up[0].startswith("backups/dropforge-") and mod.run_once(str(tmp_path), 30) == []
    fake.b["priv"]["backups/dropforge-2000-01-01.archive.gz"] = (b"old", datetime.now(timezone.utc) - timedelta(days=40))
    mod.run_once(str(tmp_path), 30)
    assert "backups/dropforge-2000-01-01.archive.gz" not in fake.b["priv"]
