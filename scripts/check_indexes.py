"""Checks that the app's important queries use an index instead of scanning a whole collection.
Runs MongoDB's explain() on each query with a real value from your data and prints the plan.

    docker compose exec api python scripts/check_indexes.py

OK   = the query walks an index (fast at any size)
SCAN = the query reads the whole collection (slow as data grows) - tell Claude which line
"""
import os
import sys

from pymongo import MongoClient

db = MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"))[os.environ.get("DB_NAME", "dropforge")]


def sample(coll, field):
    d = db[coll].find_one({field: {"$exists": True, "$ne": None}}, {field: 1})
    if not d:
        return None
    v = d
    for part in field.split("."):
        v = v.get(part) if isinstance(v, dict) else None
    return v


def stages(plan):
    out = [plan.get("stage")]
    for k in ("inputStage", "queryPlan"):
        if isinstance(plan.get(k), dict):
            out += stages(plan[k])
    for p in plan.get("inputStages", []):
        out += stages(p)
    return [s for s in out if s]


def check(label, coll, flt, sort=None):
    total = db[coll].estimated_document_count()
    cmd = {"find": coll, "filter": flt, "limit": 50}
    if sort:
        cmd["sort"] = dict(sort)
    try:
        ex = db.command("explain", cmd, verbosity="executionStats")
    except Exception as e:
        print(f"  ?    {label}: {e}")
        return True
    st = stages(ex["queryPlanner"]["winningPlan"])
    examined = ex["executionStats"]["totalDocsExamined"]
    scan = "COLLSCAN" in st
    if scan and total < 50:
        verdict = "ok*"  # too little data for the planner to bother; re-run when there is more
    else:
        verdict = "SCAN" if scan else "OK"
    print(f"  {verdict:<4} {label:<46} examined {examined:>7} of {total:>8}  ({' > '.join(st)})")
    return verdict != "SCAN"


u_id, u_phone = sample("users", "id"), sample("users", "phone")
o_user, o_rzp, o_id = sample("orders", "user_id"), sample("orders", "razorpay_order_id"), sample("orders", "id")
f_path, d_user, p_id = sample("files", "path"), sample("designs", "user_id"), sample("catalog_products", "id")
c_user, sh_slug, code = sample("creators", "user_id"), sample("shares", "slug"), sample("discounts", "code")

print("Index check (explain on live data)\n")
ok = all([
    check("sign-in: user by phone", "users", {"phone": u_phone or "+910000000000"}),
    check("every request: user by id", "users", {"id": u_id or "x"}),
    check("birthday job: dob_md", "users", {"dob_md": {"$in": ["06-05"]}}),
    check("my orders (newest first)", "orders", {"user_id": o_user or "x"}, [("created_at", -1)]),
    check("order by id", "orders", {"id": o_id or "x"}),
    check("payment confirmation: razorpay id", "orders", {"razorpay_order_id": o_rzp or "x"}),
    check("admin 'to do' tab", "orders", {"status": {"$in": ["paid", "printed"]}}, [("created_at", -1)]),
    check("dashboard: orders by day", "orders", {"day": {"$gte": "2026-01-01"}}),
    check("image request: file by path", "files", {"path": f_path or "x"}),
    check("my designs", "designs", {"user_id": d_user or "x"}, [("updatedAt", -1)]),
    check("product page", "catalog_products", {"id": p_id or "x"}),
    check("shop listing", "catalog_products", {"status": "active"}, [("created_at", -1), ("id", 1)]),
    check("creator by user", "creators", {"user_id": c_user or "x"}),
    check("share link", "shares", {"slug": sh_slug or "x"}),
    check("discount code", "discounts", {"code": code or "X"}),
    check("sign-in limit: codes per phone", "otp_sends", {"phone": u_phone or "x", "at": {"$gte": __import__("datetime").datetime(2026, 1, 1)}}),
    check("push broadcast: promo devices", "push_tokens", {"promo": {"$ne": False}}),
])
print("\nUnique indexes in place:")
for coll, field in [("users", "phone"), ("users", "id"), ("orders", "id"), ("files", "path"), ("discounts", "code"), ("shares", "slug")]:
    uniq = any(ix.get("unique") and list(ix["key"].keys()) == [field] for ix in db[coll].list_indexes())
    print(f"  {'yes' if uniq else 'NO '}  {coll}.{field}" + ("" if uniq else "   <- duplicates exist? see the api logs"))
print("\nAll good." if ok else "\nSome queries scan whole collections - send this output to Claude.")
sys.exit(0 if ok else 1)
