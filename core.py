"""Shared backend plumbing: env, Mongo, JWT auth and admin phone checks."""
import contextvars
import ipaddress
import os
import time
from pathlib import Path
from typing import Optional

import jwt
from dotenv import load_dotenv
from fastapi import Header, HTTPException, Query, Request
from motor.motor_asyncio import AsyncIOMotorClient

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

client = AsyncIOMotorClient(os.environ["MONGO_URL"])
db = client[os.environ["DB_NAME"]]

JWT_SECRET = os.environ["JWT_SECRET"]
TOKEN_DAYS = 30


def make_token(user_id: str) -> str:
    return jwt.encode({"sub": user_id, "exp": int(time.time()) + TOKEN_DAYS * 86400}, JWT_SECRET, algorithm="HS256")


def _decode(token: str) -> Optional[str]:
    if not token:
        return None
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"]).get("sub")
    except jwt.PyJWTError:
        return None


async def current_user(authorization: Optional[str] = Header(None), token: Optional[str] = Query(None)) -> dict:
    user = await optional_user(authorization, token)
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in")
    return user


async def optional_user(authorization: Optional[str] = Header(None), token: Optional[str] = Query(None)) -> Optional[dict]:
    raw = (authorization or "").removeprefix("Bearer ").strip() or (token or "")
    uid = _decode(raw) if raw else None
    if not uid:
        return None
    return await db.users.find_one({"id": uid}, {"_id": 0})


_CLOUDFLARE = [ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18",
    "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22", "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32",
    "2405:b500::/32", "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
)]


def _is_proxy(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return not a.is_global or any(a in n for n in _CLOUDFLARE)


def client_ip(request: Request) -> str:
    """Visitor address from X-Forwarded-For: drop our load balancer (right-most), skip Cloudflare/private hops,
    take the next one. Entries further left are client-controlled and ignored."""
    chain = [p.strip() for p in (request.headers.get("x-forwarded-for") or "").split(",") if p.strip()]
    for p in reversed(chain[:-1]):
        if not _is_proxy(p):
            return p
    return chain[0] if chain else (request.client.host if request.client else "?")


# Admin access = signed in (OTP) with a phone listed in ADMIN_PHONES (comma separated).
ADMIN_PHONES = {p.strip() for p in os.environ.get("ADMIN_PHONES", "").split(",") if p.strip()}
_admin_phone: contextvars.ContextVar = contextvars.ContextVar("admin_phone", default=None)


def is_admin_phone(phone: Optional[str]) -> bool:
    return bool(phone) and phone in ADMIN_PHONES


async def admin_context(request: Request, call_next):
    """Resolves the signed-in phone for admin requests so check_pin can require the admin's own login."""
    phone = None
    if request.url.path.startswith(("/api/admin", "/api/config")) or "x-admin-pin" in request.headers:
        uid = _decode((request.headers.get("authorization") or "").removeprefix("Bearer ").strip())
        if uid:
            u = await db.users.find_one({"id": uid}, {"_id": 0, "phone": 1})
            phone = (u or {}).get("phone")
    tok = _admin_phone.set(phone)
    try:
        return await call_next(request)
    finally:
        _admin_phone.reset(tok)


def check_pin(_unused: Optional[str], ip: str):
    """Admin gate (name kept for the many call sites): the signed-in phone must be in ADMIN_PHONES. No PIN."""
    if not is_admin_phone(_admin_phone.get()):
        raise HTTPException(status_code=403, detail="Admin access needs the admin phone login")
