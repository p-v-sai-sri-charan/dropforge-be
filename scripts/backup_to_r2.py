"""Off-site copies of the daily mongodump: uploads each new archive in /backups to R2_BUCKET under backups/ (dated by
the file's time) and deletes copies older than BACKUP_KEEP_DAYS (default 30). Runs as the `offsite` compose service.

    python scripts/backup_to_r2.py           # once
    python scripts/backup_to_r2.py --loop    # every few hours, forever
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402

PREFIX = "backups/"


def remote_name(path: str) -> str:
    stamp = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).strftime("%Y-%m-%d")
    base = os.path.basename(path).split("-")[0] or "db"
    return f"{PREFIX}{base}-{stamp}.archive.gz"


def run_once(folder: str, keep_days: int) -> list:
    b = os.environ["R2_BUCKET"]  # always the private bucket
    have = {o["Key"]: o for o in storage.r2_list(b, PREFIX)}
    uploaded = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not name.endswith(".archive.gz") or time.time() - os.path.getmtime(path) < 300:
            continue  # not a dump, or still being written
        key = remote_name(path)
        if key in have and have[key].get("Size") == os.path.getsize(path):
            continue
        with open(path, "rb") as f:
            storage.client().upload_fileobj(f, b, key, ExtraArgs={"ContentType": "application/gzip"})
        uploaded.append(key)
        print(f"uploaded {key}", flush=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    old = [k for k, o in have.items() if o["LastModified"] < cutoff]
    if old:
        storage.r2_delete(b, old)
        print(f"removed {len(old)} copies older than {keep_days} days", flush=True)
    return uploaded


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default="/backups")
    p.add_argument("--loop", action="store_true")
    a = p.parse_args()
    keep = int(os.environ.get("BACKUP_KEEP_DAYS", "30"))
    if not storage.r2_ready():
        print("R2 not configured: off-site backups are off", flush=True)
        if a.loop:
            while True:
                time.sleep(86400)
        sys.exit(0)
    while True:
        try:
            run_once(a.dir, keep)
        except Exception as e:  # keep trying next round
            print(f"off-site backup failed: {e}", flush=True)
        if not a.loop:
            break
        time.sleep(6 * 3600)
