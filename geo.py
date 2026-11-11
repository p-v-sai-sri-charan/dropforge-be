"""IP -> country -> selling currency, so guests see local prices without choosing. Cached in Redis."""
import ipaddress
import logging
from typing import Optional

import requests
from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

import cache
from core import client_ip
from regions import COUNTRY_CURRENCY

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


def _lookup(ip: str) -> Optional[str]:
    try:
        r = requests.get(f"https://ipwho.is/{ip}", params={"fields": "success,country_code"}, timeout=3)
        d = r.json()
        return d.get("country_code") if d.get("success") else None
    except Exception as e:
        logger.warning("geo lookup failed: %s", e)
        return None


async def country_for(request: Request) -> Optional[str]:
    cf = (request.headers.get("cf-ipcountry") or "").upper()  # set when Cloudflare fronts the API
    if len(cf) == 2 and cf.isalpha() and cf not in ("XX", "T1"):
        return cf
    ip = client_ip(request)
    try:
        if not ipaddress.ip_address(ip).is_global:
            return None
    except ValueError:
        return None
    hit = await cache.cache_get(f"geo:{ip}")
    if hit is not None:
        return hit or None
    cc = await run_in_threadpool(_lookup, ip)
    await cache.cache_set(f"geo:{ip}", cc or "", ttl=86400 if cc else 600)
    return cc


@router.get("/geo")
async def geo(request: Request):
    """{country, currency}; currency is null when the visitor's country isn't one we sell in."""
    cc = await country_for(request)
    return {"country": cc, "currency": COUNTRY_CURRENCY.get(cc) if cc else None}
