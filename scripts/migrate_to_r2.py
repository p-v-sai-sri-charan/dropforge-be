"""Copy every Cloudinary image into R2 and switch its record over. Links don't change (/api/files/cld/... keeps working,
it is just read from R2 now). Safe to re-run: files already on R2 are skipped. The Cloudinary copy is kept unless
--delete-source is given, so you can roll back by setting provider back to "cloudinary".

Run on the server:   docker compose exec api python scripts/migrate_to_r2.py --dry-run
                     docker compose exec api python scripts/migrate_to_r2.py
                     docker compose exec api python scripts/migrate_to_r2.py --delete-source   (later, once happy)
"""
import argparse
import asyncio
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.concurrency import run_in_threadpool  # noqa: E402

import storage  # noqa: E402
from core import db  # noqa: E402
from files_routes import EXT, cld_fetch, cloudinary_ready  # noqa: E402


def _copy(rec: dict, b: str, key: str) -> str:
    data = cld_fetch(rec)
    storage.r2_put(b, key, data, rec["content_type"], "public, max-age=31536000, immutable" if rec.get("public") else "private, max-age=86400")
    back = storage.r2_get(b, key)  # read it back before trusting it
    if hashlib.sha256(back).digest() != hashlib.sha256(data).digest():
        raise RuntimeError("copy differs from the original")
    return f"{len(data)} bytes"


async def main(args):
    if not (storage.r2_ready() and cloudinary_ready()):
        sys.exit("Set both the R2_* and CLOUDINARY_* variables first.")
    q = {"provider": "cloudinary", "is_deleted": False}
    total = await db.files.count_documents(q)
    print(f"{total} Cloudinary files to move{' (dry run)' if args.dry_run else ''}")
    done = failed = 0
    async for rec in db.files.find(q).limit(args.limit or 0):
        public = bool(rec.get("public"))
        b = storage.bucket(public)
        key = f"{'public' if public else 'private'}/{rec['owner_id']}/{rec['id']}.{EXT.get(rec['content_type'], 'bin')}"
        if args.dry_run:
            print(f"  would copy {rec['path']} -> {b}/{key}")
            continue
        try:
            info = await run_in_threadpool(_copy, rec, b, key)
        except Exception as e:
            failed += 1
            print(f"  FAILED {rec['path']}: {e}")
            continue
        cld = {k: rec.get(k) for k in ("public_id", "format", "delivery_type", "cloud_url")}
        await db.files.update_one({"_id": rec["_id"]}, {"$set": {"provider": "r2", "bucket": b, "key": key, "cloudinary": cld}})
        done += 1
        print(f"  ok {rec['path']} ({info})")
    if args.delete_source:
        import cloudinary.uploader

        async for rec in db.files.find({"provider": "r2", "cloudinary.public_id": {"$exists": True}, "cloudinary_deleted": {"$ne": True}}):
            c = rec["cloudinary"]
            if args.dry_run:
                print(f"  would delete Cloudinary {c['public_id']}")
                continue
            try:
                await run_in_threadpool(lambda: cloudinary.uploader.destroy(c["public_id"], resource_type="image", type=c["delivery_type"], invalidate=True))
                await db.files.update_one({"_id": rec["_id"]}, {"$set": {"cloudinary_deleted": True}})
            except Exception as e:
                print(f"  could not delete {c['public_id']}: {e}")
    print(f"moved {done}, failed {failed}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="move at most this many (0 = all)")
    p.add_argument("--delete-source", action="store_true", help="also delete Cloudinary copies of files already on R2")
    asyncio.run(main(p.parse_args()))
