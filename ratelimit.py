"""Per-IP request rate limiting backed by Redis (shared across workers)."""
import os

from fastapi import Request
from fastapi.responses import JSONResponse

import cache
from core import client_ip

DEFAULT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "240"))
# (path prefix, bucket name, requests per minute); first match wins, others use the default bucket.
RULES = [
    ("/api/auth", "auth", int(os.environ.get("RATE_LIMIT_AUTH_PER_MIN", "20"))),
    ("/api/ai", "ai", int(os.environ.get("RATE_LIMIT_AI_PER_MIN", "20"))),
    ("/api/upload", "upload", int(os.environ.get("RATE_LIMIT_UPLOAD_PER_MIN", "30"))),
    ("/api/admin", "admin", int(os.environ.get("RATE_LIMIT_ADMIN_PER_MIN", "60"))),
]
EXEMPT = ("/api/health",)


async def rate_limit(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or path in EXEMPT or not path.startswith("/api"):
        return await call_next(request)
    bucket, limit = "all", DEFAULT_PER_MIN
    for prefix, name, per_min in RULES:
        if path.startswith(prefix):
            bucket, limit = name, per_min
            break
    res = await cache.hit(f"{bucket}:{client_ip(request)}", 60)
    if res and res[0] > limit:
        return JSONResponse({"detail": "Too many requests, slow down"}, status_code=429, headers={"Retry-After": str(res[1])})
    return await call_next(request)
