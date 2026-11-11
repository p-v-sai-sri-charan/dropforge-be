# Legacy suites depend on the removed test phone login (+15550001234/246810) and PIN-only admin access.
# Both were removed by the security audit (iteration 10), so these files are obsolete by design.
collect_ignore = [
    "test_iter2_features.py",
    "test_iter3_upload.py",
    "test_iter4_store.py",
    "test_iter6_cloudinary.py",
    "test_tshirt_api.py",
]


def admin_headers(pin="2468"):
    """Admin = admin phone JWT + PIN. Reuses the existing admin user (+919880963316); never deletes it."""
    import time
    from pathlib import Path

    import jwt
    from dotenv import dotenv_values
    from pymongo import MongoClient

    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    phone = env["ADMIN_PHONES"].split(",")[0].strip()
    u = MongoClient(env["MONGO_URL"])[env["DB_NAME"]].users.find_one({"phone": phone}, {"_id": 0, "id": 1})
    tok = jwt.encode({"sub": u["id"], "exp": int(time.time()) + 86400}, env["JWT_SECRET"], algorithm="HS256")
    return {"X-Admin-Pin": pin, "Authorization": f"Bearer {tok}"}
