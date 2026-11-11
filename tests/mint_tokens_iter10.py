# Helper: mint admin (existing doc) + TEST non-admin JWTs for frontend admin-gating tests
import os, sys, time, uuid, json
from pymongo import MongoClient
import jwt
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
db = MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
secret = os.environ["JWT_SECRET"]

if len(sys.argv) > 1 and sys.argv[1] == "cleanup":
    r = db.users.delete_one({"id": sys.argv[2]})
    print("deleted", r.deleted_count)
    sys.exit(0)

admin = db.users.find_one({"phone": "+919880963316"}, {"_id": 0})
if not admin:
    print("NO ADMIN USER DOC"); sys.exit(1)
nid = "TEST_" + uuid.uuid4().hex[:12]
db.users.insert_one({"id": nid, "phone": "+15550009877", "created_at": time.time()})
now = int(time.time())
mk = lambda sub: jwt.encode({"sub": sub, "exp": now + 86400}, secret, "HS256")
json.dump({"admin": mk(admin["id"]), "user": mk(nid), "user_id": nid}, open("/tmp/tokens.json", "w"))
print("ok", admin["id"], nid)
