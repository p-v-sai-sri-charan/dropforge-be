# Iteration 12 UI setup: mint TEST user token + create a cloud design with a SHIELD badge layer. 'cleanup' removes them.
import json, os, sys, time, uuid
import jwt, requests
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv("/app/backend/.env")
load_dotenv("/app/frontend/.env")
BASE = os.environ["EXPO_PUBLIC_BACKEND_URL"].rstrip("/")
db = MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
admin = db.users.find_one({"phone": "+919880963316"}, {"_id": 0})
mk = lambda sub: jwt.encode({"sub": sub, "exp": int(time.time()) + 86400}, os.environ["JWT_SECRET"], "HS256")
AH = {"Authorization": f"Bearer {mk(admin['id'])}", "X-Admin-Pin": "2468"}

if len(sys.argv) > 1 and sys.argv[1] == "cleanup":
    s = json.load(open("/tmp/iter12.json"))
    h = {"Authorization": f"Bearer {s['token']}"}
    print("design del", requests.delete(f"{BASE}/api/designs/{s['design']}", headers=h).status_code)
    print("user del", db.users.delete_one({"id": s["user_id"]}).deleted_count)
    for t in ["ring", "vintage", "ribbon", "shield", "stamp", "seal", "sunburst", "varsity"]:
        requests.patch(f"{BASE}/api/admin/badges/{t}", headers=AH, json={"active": True})
    print("badges", [(b["id"], b["active"]) for b in requests.get(f"{BASE}/api/admin/badges", headers=AH).json()])
    sys.exit(0)

if len(sys.argv) > 2 and sys.argv[1] == "toggle":
    print(requests.patch(f"{BASE}/api/admin/badges/{sys.argv[2]}", headers=AH, json={"active": sys.argv[3] == "on"}).json())
    sys.exit(0)

uid_ = "TEST_" + uuid.uuid4().hex[:12]
db.users.insert_one({"id": uid_, "phone": "+15550009812", "created_at": time.time()})
tok = mk(uid_)
h = {"Authorization": f"Bearer {tok}"}
b = {x["id"]: x for x in requests.get(f"{BASE}/api/badges").json()}["shield"]
meta = {"template": "shield", "top": "TEST CLUB", "middle": "LIONS", "bottom": "EST 2001", "font_id": b["font_id"], "color": "#D32F2F", "accent": None}
svg = requests.post(f"{BASE}/api/badges/render", json=meta).json()["svg"]
did = "TEST_" + uuid.uuid4().hex[:10]
layer = {"id": "l1", "kind": "badge", "src": svg, "x": 0.5, "y": 0.4, "scale": 0.6, "rotation": 0, "badge": meta}
r = requests.put(f"{BASE}/api/designs/{did}", headers=h, json={"name": "TEST_iter12", "product": "tee", "color": "#FFFFFF", "front": [layer], "back": []})
print("put", r.status_code, r.text[:200])
json.dump({"token": tok, "user_id": uid_, "design": did, "font_id": b["font_id"]}, open("/tmp/iter12.json", "w"))
print(json.dumps({"token": tok, "design": did}))
