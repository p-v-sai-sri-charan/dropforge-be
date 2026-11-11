# Iteration 13 cleanup: release stored images created by the given TEST_ owner ids (exact ids only)
import asyncio
import sys

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
from core import db  # noqa: E402
import files_routes  # noqa: E402


async def main(owners):
    files_routes.init_storage() if hasattr(files_routes, "init_storage") else None
    for o in owners:
        recs = await db.files.find({"owner_id": o}, {"_id": 0, "path": 1, "is_deleted": 1}).to_list(100)
        print(o, [(r["path"][-12:], r.get("is_deleted")) for r in recs])
        n = await files_routes.release_files([f"/api/files/{r['path']}" for r in recs if not r.get("is_deleted")])
        print("released", n)


asyncio.run(main(sys.argv[1:]))
