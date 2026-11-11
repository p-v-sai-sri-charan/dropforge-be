# Iteration 14 UI fixtures: TEST orders + designs for the 4 new garments (setup / cleanup)
import json, os, sys, uuid
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv, dotenv_values
from pymongo import MongoClient

load_dotenv("/app/backend/.env")
db = MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
API = dotenv_values("/app/frontend/.env")["EXPO_PUBLIC_BACKEND_URL"].rstrip("/") + "/api"
tok = json.load(open("/tmp/tokens.json"))
uid = tok["user_id"]
if sys.argv[1:] == ["cleanup"]:
    st = json.load(open("/tmp/iter14_ui.json"))
    print("orders", db.orders.delete_many({"id": {"$in": st["orders"]}}).deleted_count)
    h = {"Authorization": f"Bearer {tok['user']}"}
    for d in db.designs.find({"user_id": uid}, {"_id": 0, "id": 1}):
        print("design", d["id"], requests.delete(f"{API}/designs/{d['id']}", headers=h).status_code)
    sys.exit(0)
now = datetime.now(timezone.utc).isoformat()
orders = []
for p in ["tee3d", "croptop", "sweatshirt", "sleeveless"]:
    oid = "TEST_" + uuid.uuid4().hex[:10]
    db.orders.insert_one({"id": oid, "user_id": uid, "phone": "+15550009877", "kind": "design", "design_name": f"TEST_ORD_{p}",
                          "product": p, "color": "#111111", "photo": None, "size": "XXL", "qty": 1, "currency": "INR",
                          "unit_amount": 89900, "subtotal": 89900, "discount": 0, "amount": 89900, "status": "paid",
                          "shipping": {"name": "TEST", "city": "Pune", "country": "IN"},
                          "history": [{"status": "paid", "at": now}], "design_snapshot": {"front": [], "back": []}, "created_at": "2099-01-01T00:00:00"})
    orders.append(oid)
h = {"Authorization": f"Bearer {tok['user']}"}
for p in ["tee3d", "croptop", "sleeveless"]:
    did = "TEST_" + uuid.uuid4().hex[:10]
    r = requests.put(f"{API}/designs/{did}", headers=h, json={"name": f"TEST_MOD_{p}", "product": p, "color": "#111111", "front": [], "back": []})
    print(p, r.status_code)
json.dump({"orders": orders}, open("/tmp/iter14_ui.json", "w"))
print("orders", orders)
